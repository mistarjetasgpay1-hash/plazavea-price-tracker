"""Pruebas del modo continuo (--loop), herencia de configuración y carga de .env."""

from __future__ import annotations

import os
import threading

import pytest

from tracker.core.config import ConfigError, first_env, load_config
from tracker.core.runner import GracefulExit, load_env_file, run_loop

BASE = """
store: {id: tienda, name: Tienda}
telegram: {token_env: [TK_A, TK_COMUN], chat_id_env: CH_COMUN, groups: {A: {emoji: "🅰️"}}}
offers: {min_discount_pct: 50}
scraper: {api_url: x, sellers: {s: "1"}}
"""
HIJO = """
extends: base.yaml
store: {label: Hijo}
offers: {min_price: 5}
telegram: {groups: {B: {emoji: "🅱️"}}}
scraper: {categories: [{key: K, path: p}]}
"""


@pytest.fixture
def configs(tmp_path, monkeypatch):
    (tmp_path / "base.yaml").write_text(BASE)
    (tmp_path / "hijo.yaml").write_text(HIJO)
    monkeypatch.setenv("TK_COMUN", "token-comun")
    monkeypatch.setenv("CH_COMUN", "chat")
    monkeypatch.delenv("TK_A", raising=False)
    return tmp_path


class FakeEvent:
    """Sustituye a threading.Event: registra las esperas sin dormir."""

    def __init__(self):
        self.esperas: list[float] = []
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, segundos):
        self.esperas.append(round(segundos, 1))
        return self._set


class Reloj:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ----------------------------------------------------------------- config
def test_extends_merges_deeply(configs):
    cfg = load_config(configs / "hijo.yaml")
    assert cfg.offers.min_discount_pct == 50 and cfg.offers.min_price == 5
    assert set(cfg.telegram.groups) == {"A", "B"}
    assert cfg.scraper["sellers"] == {"s": "1"} and cfg.scraper["categories"][0]["key"] == "K"
    assert cfg.display_name == "Tienda · Hijo"


def test_env_fallback_list(configs, monkeypatch):
    assert load_config(configs / "hijo.yaml").telegram.token == "token-comun"
    monkeypatch.setenv("TK_A", "token-propio")
    assert load_config(configs / "hijo.yaml").telegram.token == "token-propio"
    assert first_env(["NO_EXISTE_1", "NO_EXISTE_2"]) == ""


def test_extends_cycle_is_detected(tmp_path):
    (tmp_path / "a.yaml").write_text("extends: b.yaml\n")
    (tmp_path / "b.yaml").write_text("extends: a.yaml\n")
    with pytest.raises(ConfigError, match="circular"):
        load_config(tmp_path / "a.yaml", require_secrets=False)


def test_load_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comentario\nNUEVA_VAR='hola mundo'\nexport OTRA=1\nYA_DEFINIDA=nuevo\nlinea_invalida\n")
    monkeypatch.delenv("NUEVA_VAR", raising=False)
    monkeypatch.delenv("OTRA", raising=False)
    monkeypatch.setenv("YA_DEFINIDA", "original")
    assert load_env_file(env) == 2
    assert os.environ["NUEVA_VAR"] == "hola mundo" and os.environ["OTRA"] == "1"
    assert os.environ["YA_DEFINIDA"] == "original"          # no pisa variables existentes
    assert load_env_file(tmp_path / "no_existe") == 0


# ------------------------------------------------------------------- loop
def test_loop_fixed_cadence_and_survives_errors(configs):
    reloj, evento, corridas = Reloj(), FakeEvent(), []
    duraciones = iter([70, 10, 400, 5])

    def corrida(cfg, dry_run, max_alerts):
        corridas.append(cfg.display_name)
        reloj.t += next(duraciones)
        if len(corridas) == 2:
            raise RuntimeError("se cayó la red")

    codigo = run_loop(configs / "hijo.yaml", interval=300, stop_event=evento, run_once=corrida,
                      monotonic=reloj, max_cycles=4, install_signals=False)
    assert codigo == 0 and len(corridas) == 4               # el error del ciclo 2 no detuvo el bucle
    assert evento.esperas == [230, 290, 0]                   # inicio a inicio; si se pasa, sin espera


def test_loop_stops_on_event_and_graceful_exit(configs):
    evento = FakeEvent()

    def corrida(cfg, dry_run, max_alerts):
        raise GracefulExit()

    assert run_loop(configs / "hijo.yaml", interval=300, stop_event=evento, run_once=corrida,
                    monotonic=Reloj(), install_signals=False) == 0
    assert evento.esperas == []

    real = threading.Event()
    real.set()
    llamadas = []
    run_loop(configs / "hijo.yaml", interval=300, stop_event=real,
             run_once=lambda *a: llamadas.append(1), install_signals=False)
    assert llamadas == []


def test_loop_config_errors(configs, monkeypatch):
    # Sin secretos al iniciar → código 2 (PM2 no reintenta en bucle)
    monkeypatch.delenv("TK_COMUN")
    assert run_loop(configs / "hijo.yaml", interval=300, stop_event=FakeEvent(),
                    run_once=lambda *a: None, install_signals=False) == 2
    monkeypatch.setenv("TK_COMUN", "t")

    # Config que se rompe a mitad de camino → se sigue con la última válida
    vistos = []

    def corrida(cfg, dry_run, max_alerts):
        vistos.append(cfg.offers.min_price)
        (configs / "hijo.yaml").write_text("extends: base.yaml\noffers: {clave_mala: 1}\n")

    run_loop(configs / "hijo.yaml", interval=300, stop_event=FakeEvent(), run_once=corrida,
             monotonic=Reloj(), max_cycles=2, install_signals=False)
    assert vistos == [5, 5]


def test_cli_rejects_tiny_interval(configs):
    from tracker.__main__ import main

    assert main(["--config", str(configs / "hijo.yaml"), "--loop", "5"]) == 2


def test_exclusive_lock_blocks_second_instance(tmp_path):
    from tracker.state.store import exclusive_lock

    ruta = tmp_path / "state" / "bot.json"
    with exclusive_lock(ruta) as primero:
        assert primero
        with exclusive_lock(ruta) as segundo:
            assert not segundo
    with exclusive_lock(ruta) as de_nuevo:
        assert de_nuevo

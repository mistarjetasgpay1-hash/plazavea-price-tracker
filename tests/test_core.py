"""Pruebas del núcleo compartido: precios, filtros, historial con TTL y formato."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import make_offer
from tracker.telegram import formatter
from tracker.core.config import ConfigError, OffersConfig, load_config
from tracker.core.filters import is_official, rejection_reason
from tracker.core.pricing import discount_pct, parse_price
from tracker.state.store import StateStore


# ------------------------------------------------------------------ precios
@pytest.mark.parametrize("raw, esperado", [
    ("1,699", 1699.0), ("S/ 1,699.90", 1699.9), ("24,90", 24.9), ("24.90", 24.9),
    (["1,799", "1,699"], 1699.0), (12, 12.0), ("", 0.0), (None, 0.0), ("1.234,56", 1234.56),
])
def test_parse_price(raw, esperado):
    assert parse_price(raw) == pytest.approx(esperado)


def test_discount_pct():
    assert discount_pct(4199, 1799) == pytest.approx(57.16, abs=0.01)
    assert discount_pct(10, 12) == 0.0
    assert discount_pct(0, 5) == 0.0


# ------------------------------------------------------------------ filtros
def test_is_official():
    assert is_official(make_offer(seller_id="1"), frozenset({"1", "oechsle"}))
    assert not is_official(make_offer(seller_id="promart"), frozenset({"1", "oechsle"}))


def test_rejection_reasons():
    cfg = OffersConfig(min_discount_pct=50, min_price=1, exclude_keywords=["Reacondicionado"])
    assert rejection_reason(make_offer(list_price=30, price=12), cfg) is None          # 60 %
    assert rejection_reason(make_offer(list_price=30, price=20), cfg) == "descuento_bajo"
    assert rejection_reason(make_offer(list_price=1.5, price=0.5), cfg) == "precio_minimo"
    assert rejection_reason(make_offer(stock=0), cfg) == "sin_stock"
    assert rejection_reason(make_offer(name="Laptop REACONDICIONADO"), cfg) == "palabra_excluida"
    # umbral propio de la categoría
    assert rejection_reason(make_offer(list_price=30, price=20, min_discount=30), cfg) is None


# ---------------------------------------------------------- historial (TTL)
class Reloj:
    def __init__(self):
        self.ahora = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.ahora


def test_state_new_drop_and_ttl(tmp_path):
    reloj = Reloj()
    ruta = tmp_path / "s.json"
    st = StateStore(ruta, ttl_days=3, drop_pct=3, store_id="pv", clock=reloj)
    st.load()
    assert st.check("pv:1", 10.0) == "new"
    st.mark_notified("pv:1", 10.0, 50.0)
    assert st.check("pv:1", 10.0) is None          # mismo precio
    assert st.check("pv:1", 9.8) is None           # bajó solo 2 %
    assert st.check("pv:1", 9.7) == "drop"         # bajó 3 %
    assert st.check("pv:1", 12.0) is None          # subió
    assert st.save()

    reloj.ahora += timedelta(days=2, hours=23)
    st2 = StateStore(ruta, ttl_days=3, drop_pct=3, store_id="pv", clock=reloj)
    st2.load()
    assert st2.check("pv:1", 10.0) is None and st2.purged == 0

    reloj.ahora += timedelta(hours=2)              # pasan los 3 días
    st3 = StateStore(ruta, ttl_days=3, drop_pct=3, store_id="pv", clock=reloj)
    st3.load()
    assert st3.purged == 1 and st3.check("pv:1", 10.0) == "new"


def test_state_keeps_first_seen_and_meta(tmp_path):
    reloj = Reloj()
    st = StateStore(tmp_path / "s.json", ttl_days=3, drop_pct=3, store_id="pv", clock=reloj)
    st.load()
    st.mark_notified("pv:1", 10, 50)
    primero = st.entries["pv:1"].first_seen
    reloj.ahora += timedelta(hours=5)
    st.mark_notified("pv:1", 9, 55)
    assert st.entries["pv:1"].first_seen == primero
    st.set_meta("failure_streak", 2)
    st.save()
    data = json.loads((tmp_path / "s.json").read_text())
    assert data["version"] == 2 and data["meta"]["failure_streak"] == 2


def test_state_legacy_import(tmp_path):
    legado = tmp_path / "notificados.json"
    legado.write_text(json.dumps(["19590824|1699.00", "45574|https://www.plazavea.com.pe/x/p"]))
    st = StateStore(tmp_path / "s.json", ttl_days=3, drop_pct=3, store_id="fa", legacy_path=legado, clock=Reloj())
    st.load()
    assert st.imported == 2
    assert st.check("fa:19590824", 1699.0) is None
    assert st.check("fa:19590824", 1500.0) == "drop"
    assert st.check("fa:45574", 5.0) is None       # sin precio previo: espera al TTL


def test_state_corrupt_file_is_backed_up(tmp_path):
    ruta = tmp_path / "s.json"
    ruta.write_text("{no es json")
    st = StateStore(ruta, ttl_days=3, drop_pct=3, store_id="pv", clock=Reloj())
    st.load()
    assert st.entries == {} and (tmp_path / "s.json.corrupt").exists()


# ------------------------------------------------------------------ formato
def test_caption_escapes_html_and_fits():
    o = make_offer(name="Salsa <Picante> & Co " + "x" * 2000, card_price=10.0)
    texto = formatter.offer_caption(o, reason="drop", previous_price=14.0, store_name="Plaza Vea",
                                    hashtag_prefix="PlazaVea", emoji="🛒")
    assert len(texto) <= 1024
    assert "&lt;Picante&gt; &amp; Co" in texto
    assert "#PlazaVea_Alimentos #Abarrotes" in texto
    assert "Bajó más" in texto and "💳 Tarjeta: <b>S/ 10.00</b>" in texto


def test_hashtag_strips_accents():
    assert formatter.hashtag("PlazaVea_Lácteos y Huevos") == "PlazaVea_Lacteos_y_Huevos"


def test_digest_respects_length():
    items = [(make_offer(product_id=str(i), name="Producto " * 10), "new") for i in range(200)]
    texto, incluidas = formatter.digest_message(items, store_name="Plaza Vea", hashtag_prefix="PlazaVea")
    assert len(texto) <= 4096 and 0 < len(incluidas) < 200


# ------------------------------------------------------------------ config
SCRAPER_MIN = "scraper: {api_url: x, sellers: {s: \"1\"}, categories: [{key: K, path: p}]}\n"


def test_config_requires_secrets(tmp_path, monkeypatch):
    ruta = tmp_path / "c.yaml"
    ruta.write_text("store: {id: t}\ntelegram: {token_env: X_TOKEN, chat_id_env: X_CHAT}\n" + SCRAPER_MIN)
    monkeypatch.delenv("X_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        load_config(ruta)
    monkeypatch.setenv("X_TOKEN", "t")
    monkeypatch.setenv("X_CHAT", "c")
    assert load_config(ruta).telegram.token == "t"


def test_config_rejects_unknown_keys(tmp_path):
    ruta = tmp_path / "c.yaml"
    ruta.write_text("store: {id: t}\noffers: {min_discount: 10}\n" + SCRAPER_MIN)
    with pytest.raises(ConfigError, match="min_discount"):
        load_config(ruta, require_secrets=False)

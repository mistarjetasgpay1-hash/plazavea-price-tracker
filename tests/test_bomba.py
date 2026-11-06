"""Bot 4 (ofertas bomba > 69 %): reenvío, umbral, historial y tolerancia a fallos."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_offer
from tests.test_io import FakeSession, Resp
from tests.test_pipeline import FakeScraper, FakeNotifier, PhasedScraper, _ofertas, _state
from tracker.core.bomba import BombaForwarder
from tracker.core.config import ConfigError, load_config
from tracker.core.pipeline import RunReport, collect_candidates, notify, run_phases
from tracker.telegram.client import TelegramNotifier

RAIZ = Path(__file__).resolve().parents[1]


class Bot4:
    """Telegram falso del bot 4: registra (caption, thread_id, foto)."""

    def __init__(self, ok=True, verify=True, boom=False, error="HTTP 403: Forbidden: bot was blocked by the user"):
        self.ok, self.verify, self.boom, self.error = ok, verify, boom, error
        self.offers, self.checks = [], 0
        self.available, self.last_error, self.closed = True, "", False

    def check_connection(self):
        self.checks += 1
        if self.verify is False:
            self.last_error = "getChat HTTP 400: Bad Request: chat not found"
        return self.verify

    def send_offer(self, caption, *, photo_url="", thread_id=None, photo_loader=None):
        if self.boom:
            raise RuntimeError("se cayó")
        self.offers.append((caption, thread_id, photo_url))
        if not self.ok:
            self.last_error = self.error
        return self.ok

    def api_summary(self):
        return f"{len(self.offers)} llamadas"

    def close(self):
        self.closed = True


def _bomba(cfg, notifier=None, **kw):
    cfg.bomba.token, cfg.bomba.chat_id = "444:abc", "-1004"
    return BombaForwarder(cfg, notifier=notifier if notifier is not None else Bot4(), **kw)


def _mezcla():
    # list_price 30: 6 → 80 %, 7.5 → 75 %, 9 → 70 %, 9.1 → 69,67 % ("-69 %"), 12 → 60 %
    return [make_offer(product_id="p1", name="Bomba Ochenta", price=6),
            make_offer(product_id="p2", name="Bomba Setenta y cinco", price=7.5),
            make_offer(product_id="p3", name="Bomba Setenta", price=9),
            make_offer(product_id="p4", name="Casi Bomba", price=9.1),
            make_offer(product_id="p5", name="Oferta Normal", price=12)]


# ----------------------------------------------------------------------- umbral
@pytest.mark.parametrize("precio, esperado", [(9, True), (6, True), (9.1, False), (9.3, False), (12, False)])
def test_threshold_is_strictly_above_69_on_the_displayed_percent(make_cfg, precio, esperado):
    bomba = _bomba(make_cfg())
    assert bomba.is_bomba(make_offer(price=precio, qualified_by="online")) is esperado


def test_threshold_uses_the_headline_percent_of_card_offers(make_cfg):
    bomba = _bomba(make_cfg())
    tarjeta = make_offer(price=20, card_price=8, qualified_by="tarjeta")         # 73 % con tarjeta
    online = make_offer(price=20, card_price=8, qualified_by="online")           # 33 % online
    assert bomba.is_bomba(tarjeta) and not bomba.is_bomba(online)


# ------------------------------------------------------------------- reenvío
def test_forwards_bombs_from_photos_and_digests_only_after_main_accepts(make_cfg):
    cfg = make_cfg()                       # max_individual_alerts=2, digest_max_items=3, thread 7
    st, rep, tg, bot4 = _state(cfg), RunReport(), FakeNotifier(), Bot4()
    scraper = FakeScraper(cfg, _mezcla())
    cands = collect_candidates(scraper, cfg, st, rep)
    notify(cands, cfg=cfg, state=st, notifier=tg, scraper=scraper, report=rep, bomba=_bomba(cfg, bot4))

    # El canal habitual no cambia: 2 con foto + 3 en un resumen, todo al historial.
    assert rep.sent_individual == 2 and rep.sent_digest == 3 and rep.failed_sends == 0
    assert len(tg.offers) == 2 and len(tg.messages) == 1
    assert set(st.entries) == {f"test:p{i}" for i in range(1, 6)}
    # Bot 4: solo > 69 % (p3 salió en el resumen y llega con foto); sin el hilo del chat habitual.
    nombres = [c for c, _, _ in bot4.offers]
    assert len(nombres) == 3
    assert "Bomba Ochenta" in nombres[0] and "Bomba Setenta y cinco" in nombres[1] and "Bomba Setenta" in nombres[2]
    assert not any("Casi Bomba" in c or "Oferta Normal" in c for c in nombres)
    assert all(t is None and foto for _, t, foto in bot4.offers)
    assert bot4.checks == 1                # getMe/getChat una sola vez, antes del primer reenvío


def test_history_prevents_spam_and_price_drops_reach_both_channels(make_cfg):
    cfg = make_cfg()
    st = _state(cfg)
    rep, tg, bot4 = RunReport(), FakeNotifier(), Bot4()
    scraper = FakeScraper(cfg, _mezcla())
    notify(collect_candidates(scraper, cfg, st, rep), cfg=cfg, state=st, notifier=tg, scraper=scraper,
           report=rep, bomba=_bomba(cfg, bot4))
    assert len(bot4.offers) == 3

    # Siguiente corrida con las mismas ofertas: ya están en el historial → nada a ningún canal.
    rep2, tg2, bot4b = RunReport(), FakeNotifier(), Bot4()
    scraper2 = FakeScraper(cfg, _mezcla())
    cands2 = collect_candidates(scraper2, cfg, st, rep2)
    notify(cands2, cfg=cfg, state=st, notifier=tg2, scraper=scraper2, report=rep2, bomba=_bomba(cfg, bot4b))
    assert cands2 == [] and bot4b.offers == [] and tg2.offers == []

    # Una bomba baja ≥3 %: re-aviso en el canal habitual y en el bot 4, con el precio anterior.
    rep3, tg3, bot4c = RunReport(), FakeNotifier(), Bot4()
    scraper3 = FakeScraper(cfg, [make_offer(product_id="p1", name="Bomba Ochenta", price=5)])
    notify(collect_candidates(scraper3, cfg, st, rep3), cfg=cfg, state=st, notifier=tg3, scraper=scraper3,
           report=rep3, bomba=_bomba(cfg, bot4c))
    assert len(tg3.offers) == 1 and len(bot4c.offers) == 1
    assert "Bajó más" in bot4c.offers[0][0] and "S/ 6.00" in bot4c.offers[0][0]


def test_drop_in_a_digest_keeps_the_previous_price_for_bot4(make_cfg):
    cfg = make_cfg()
    cfg.telegram.max_individual_alerts = 0          # todo va en resúmenes
    st = _state(cfg)
    st.mark_notified("test:p1", 6.0, 80.0, 6.0)
    rep, tg, bot4 = RunReport(), FakeNotifier(), Bot4()
    scraper = FakeScraper(cfg, [make_offer(product_id="p1", name="Bomba Ochenta", price=5)])
    notify(collect_candidates(scraper, cfg, st, rep), cfg=cfg, state=st, notifier=tg, scraper=scraper,
           report=rep, bomba=_bomba(cfg, bot4))
    assert rep.sent_digest == 1 and len(bot4.offers) == 1
    assert "Antes avisado a S/ 6.00" in bot4.offers[0][0]
    assert st.entries["test:p1"].price == 5.0


def test_main_channel_failure_never_reaches_bot4(make_cfg):
    cfg = make_cfg()
    st, rep, bot4 = _state(cfg), RunReport(), Bot4()
    scraper = FakeScraper(cfg, _mezcla())
    notify(collect_candidates(scraper, cfg, st, rep), cfg=cfg, state=st, notifier=FakeNotifier(ok=False),
           scraper=scraper, report=rep, bomba=_bomba(cfg, bot4))
    assert bot4.offers == [] and st.entries == {}


@pytest.mark.parametrize("bot4, fallos, sigue_activo, detalle", [
    (Bot4(ok=False), 1, False, "HTTP 403"),                                  # bot bloqueado: se desactiva
    (Bot4(ok=False, error="HTTP 400: Bad Request: wrong file"), 3, True, "HTTP 400"),   # error puntual: sigue
    (Bot4(ok=False, error="HTTP 502: Bad Gateway"), 3, True, "HTTP 502"),
    (Bot4(boom=True), 3, True, "RuntimeError"),
], ids=["http_403", "http_400", "http_502", "excepcion"])
def test_bot4_failures_do_not_touch_the_normal_flow(make_cfg, bot4, fallos, sigue_activo, detalle, caplog):
    cfg = make_cfg()
    st, rep, tg = _state(cfg), RunReport(), FakeNotifier()
    scraper = FakeScraper(cfg, _mezcla())
    bomba = _bomba(cfg, bot4)
    with caplog.at_level("INFO"):
        notify(collect_candidates(scraper, cfg, st, rep), cfg=cfg, state=st, notifier=tg, scraper=scraper,
               report=rep, bomba=bomba)
    assert rep.sent_individual == 2 and rep.sent_digest == 3 and rep.failed_sends == 0
    assert len(st.entries) == 5 and bomba.failed == fallos and bomba.sent == 0
    assert bomba.active is sigue_activo
    assert "[BOMBA] ✗ El bot 4 no aceptó -80% Bomba Ochenta" in caplog.text and detalle in caplog.text


def test_rejected_token4_disables_bot4_for_the_run(make_cfg):
    cfg = make_cfg()
    st, rep, tg, bot4 = _state(cfg), RunReport(), FakeNotifier(), Bot4(verify=False)
    scraper = FakeScraper(cfg, _mezcla())
    bomba = _bomba(cfg, bot4)
    notify(collect_candidates(scraper, cfg, st, rep), cfg=cfg, state=st, notifier=tg, scraper=scraper,
           report=rep, bomba=bomba)
    assert bot4.offers == [] and bot4.checks == 1 and not bomba.active
    assert rep.sent_individual == 2 and rep.sent_digest == 3
    assert "chat not found" in bomba.summary()


def test_missing_token4_or_chat4_keeps_the_bot_working(make_cfg, caplog):
    cfg = make_cfg()
    cfg.bomba.token, cfg.bomba.chat_id = "444:abc", ""
    bomba = BombaForwarder(cfg)
    assert not bomba.active and bomba.notifier is None
    with caplog.at_level("INFO"):
        bomba.log_status()
    assert "desactivado: CHAT_ID4 vacío(s)" in caplog.text
    st, rep, tg = _state(cfg), RunReport(), FakeNotifier()
    scraper = FakeScraper(cfg, _mezcla())
    notify(collect_candidates(scraper, cfg, st, rep), cfg=cfg, state=st, notifier=tg, scraper=scraper,
           report=rep, bomba=bomba)
    assert rep.sent_individual == 2 and rep.sent_digest == 3 and bomba.sent == 0
    assert bomba.summary().startswith("desactivado")


def test_disabled_in_config(make_cfg):
    cfg = make_cfg()
    cfg.bomba.enabled = False
    assert not _bomba(cfg).active


def test_dry_run_simulates_bot4_without_secrets_and_without_http(make_cfg, caplog):
    cfg = make_cfg()
    bomba = BombaForwarder(cfg, dry_run=True)
    assert bomba.active and isinstance(bomba.notifier, TelegramNotifier) and bomba.notifier.label == "TG4"
    with caplog.at_level("INFO"):
        assert bomba.forward(make_offer(price=6, qualified_by="online"), "caption")
    assert "[TG4] sendPhoto NO invocado (DRY_RUN" in caplog.text
    assert "[BOMBA] ✓ Reenviada al bot 4 [SIMULACIÓN]: -80%" in caplog.text
    assert bomba.notifier.http_calls == 0


def test_same_offer_is_never_forwarded_twice_in_a_run(make_cfg):
    cfg = make_cfg()
    bot4 = Bot4()
    bomba = _bomba(cfg, bot4)
    oferta = make_offer(price=6, qualified_by="online")
    assert bomba.forward(oferta, "a") and not bomba.forward(oferta, "a")
    assert len(bot4.offers) == 1


def test_bot4_long_429_pause_skips_the_rest(make_cfg):
    cfg = make_cfg()
    bot4 = Bot4()
    bomba = _bomba(cfg, bot4)
    bot4.available, bot4.last_error = False, "Telegram pidió esperar 900s (429)."
    assert not bomba.forward(make_offer(price=6, qualified_by="online"), "a")
    assert bomba.skipped == 1 and bot4.offers == []


def test_two_phase_run_forwards_bombs_once(make_cfg):
    cfg = make_cfg()
    head, rest = _ofertas()                       # 90 %, 85 %, 95 %, 60 % (+ repetida)
    st, rep, tg, bot4 = _state(cfg), RunReport(), FakeNotifier(), Bot4()
    run_phases(PhasedScraper(cfg, head, rest), cfg, st, tg, rep, bomba=_bomba(cfg, bot4))
    nombres = [c for c, _, _ in bot4.offers]
    assert len(nombres) == 3 and not any("Profunda Dos" in c for c in nombres)
    assert sum("Cabecera Uno" in c for c in nombres) == 1


# ------------------------------------------------------------- config y logs
def test_config_reads_token4_and_chat4_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1:a")
    monkeypatch.setenv("CHAT_ID", "-1")
    monkeypatch.setenv("TELEGRAM_TOKEN4", "  444:abc  ")
    monkeypatch.setenv("CHAT_ID4", "-1004")
    for nombre in ("config.general.yaml", "config.alimentos.yaml"):
        cfg = load_config(RAIZ / nombre, require_secrets=False)
        assert cfg.bomba.token == "444:abc" and cfg.bomba.chat_id == "-1004"
        assert cfg.bomba.discount_gt == 69 and cfg.bomba.enabled


def test_config_without_token4_loads_normally(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1:a")
    monkeypatch.setenv("CHAT_ID", "-1")
    monkeypatch.delenv("TELEGRAM_TOKEN4", raising=False)
    monkeypatch.setenv("CHAT_ID4", "")
    cfg = load_config(RAIZ / "config.general.yaml")          # require_secrets=True: no exige el bot 4
    assert cfg.bomba.token == "" and cfg.bomba.chat_id == ""


def test_config_rejects_unknown_bomba_keys(tmp_path):
    hijo = tmp_path / "c.yaml"
    hijo.write_text(f"extends: {RAIZ / 'config.general.yaml'}\nbomba:\n  umbral: 70\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="bomba"):
        load_config(hijo, require_secrets=False)


def test_client_label_prefixes_bot4_http_logs(caplog, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    s = FakeSession([Resp(200, {"ok": True, "result": {"message_id": 7}}),
                     Resp(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"})])
    tg = TelegramNotifier("SECRETO4", "C4", min_interval_s=0, session=s, label="TG4")
    with caplog.at_level("INFO", logger="tracker.telegram.client"):
        assert tg.send_message("uno") and not tg.send_message("dos")
    assert "[TG4] ← sendMessage · HTTP 200 · ok=true · message_id=7" in caplog.text
    assert "[TG4] ← sendMessage · HTTP 403 · ok=false · error_code=403 · Forbidden: bot was kicked" in caplog.text
    assert "SECRETO4" not in caplog.text


def test_run_logs_bomba_status_and_summary(make_cfg, monkeypatch, caplog, tmp_path):
    from tracker.core import pipeline

    resumen = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(resumen))
    monkeypatch.setenv("TELEGRAM_TOKEN4", "444:abc")
    monkeypatch.setenv("CHAT_ID4", "-1004")
    cfg = make_cfg()
    cfg.bomba.token, cfg.bomba.chat_id = "444:abc", "-1004"
    monkeypatch.setattr("tracker.scrapers.build_scraper", lambda c, meta: PhasedScraper(c, *_ofertas()))
    with caplog.at_level("INFO"):
        assert pipeline.run(cfg, dry_run=True) == 0
    texto = caplog.text
    assert "[BOMBA] Bot 4 activo: reenvía las alertas > 69 % · token: variable TELEGRAM_TOKEN4 (definida, 7" in texto
    assert texto.count("[BOMBA] ✓ Reenviada al bot 4 [SIMULACIÓN]") == 3
    assert "[TG4] Bot 4 (ofertas > 69 %) en esta corrida: activo · 3 reenviadas · 0 fallidas" in texto
    assert "444:abc" not in texto
    assert "Bot 4 · ofertas > 69 %" in resumen.read_text(encoding="utf-8")


def test_run_without_token4_is_unchanged(make_cfg, monkeypatch, caplog):
    from tracker.core import pipeline

    cfg = make_cfg()
    monkeypatch.setattr("tracker.scrapers.build_scraper", lambda c, meta: PhasedScraper(c, *_ofertas()))
    # Modo real sin bot 4: el notificador principal es falso y el bot 4 queda desactivado.
    class Principal(FakeNotifier):
        def __init__(self, *a, **kw):
            super().__init__()
            self.available, self.last_error = True, ""

        def check_connection(self):
            return True

        def api_summary(self):
            return "ok"

        def close(self):
            pass

    monkeypatch.setattr(pipeline, "TelegramNotifier", Principal)
    with caplog.at_level("INFO"):
        assert pipeline.run(cfg) == 0
    assert "[BOMBA] Bot 4 (ofertas > 69 %) desactivado: TELEGRAM_TOKEN4 y CHAT_ID4 vacío(s)" in caplog.text
    assert "Reenviada al bot 4" not in caplog.text

"""Pruebas del orquestador con un scraper y un Telegram falsos."""

from __future__ import annotations

from tests.conftest import make_offer
from tracker.scrapers.base import BaseScraper
from tracker.core.pipeline import RunReport, collect_candidates, notify, update_health
from tracker.state.store import StateStore


class FakeScraper(BaseScraper):
    store_id = "test"
    allowed_seller_ids = frozenset({"1"})

    def __init__(self, cfg, offers, fail=False):
        super().__init__(cfg, {})
        self._offers = offers
        if fail:
            self.stats.fail("HTTP 403")

    def iter_offers(self):
        for o in self._offers:
            self.stats.products_seen += 1
            yield o


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.offers, self.messages = [], []

    def send_offer(self, caption, *, photo_url="", thread_id=None, photo_loader=None):
        self.offers.append((caption, thread_id))
        return self.ok

    def send_message(self, text, *, thread_id=None, silent=False, preview=True):
        self.messages.append(text)
        return self.ok


def _state(cfg):
    st = StateStore(cfg.resolve(cfg.state.path), ttl_days=3, drop_pct=3, store_id="test")
    st.load()
    return st


def test_pipeline_filters_sorts_and_digests(make_cfg):
    cfg = make_cfg()
    offers = [
        make_offer(product_id="a", price=12),                    # 60 %
        make_offer(product_id="b", price=6),                     # 80 %
        make_offer(product_id="c", seller_id="promart"),         # marketplace → fuera
        make_offer(product_id="d", price=25),                    # 17 % → fuera
        make_offer(product_id="a", price=12),                    # duplicado
        make_offer(product_id="e", price=15),                    # 50 %
        make_offer(product_id="f", price=17),                    # 43 %
    ]
    st, rep, tg = _state(cfg), RunReport(), FakeNotifier()
    scraper = FakeScraper(cfg, offers)
    cands = collect_candidates(scraper, cfg, st, rep)
    assert [o.product_id for o, _ in cands] == ["b", "a", "e", "f"]
    assert rep.rejected == {"vendedor_no_oficial": 1, "descuento_bajo": 1, "duplicado": 1}

    notify(cands, cfg=cfg, state=st, notifier=tg, scraper=scraper, report=rep)
    assert rep.sent_individual == 2 and rep.sent_digest == 2 and rep.deferred == 0
    assert tg.offers[0][1] == 7                                   # thread_id del grupo
    assert len(tg.messages) == 1 and "Más ofertas" in tg.messages[0]
    assert set(st.entries) == {"test:a", "test:b", "test:e", "test:f"}

    # segunda corrida: nada nuevo; luego una bajada ≥3 % se vuelve a avisar
    rep2 = RunReport()
    assert collect_candidates(FakeScraper(cfg, offers), cfg, st, rep2) == []
    rep3 = RunReport()
    cands3 = collect_candidates(FakeScraper(cfg, [make_offer(product_id="a", price=11)]), cfg, st, rep3)
    assert cands3[0][1] == "drop"


def test_failed_sends_are_not_marked(make_cfg):
    cfg = make_cfg()
    st, rep = _state(cfg), RunReport()
    scraper = FakeScraper(cfg, [make_offer(product_id="z")])
    cands = collect_candidates(scraper, cfg, st, rep)
    notify(cands, cfg=cfg, state=st, notifier=FakeNotifier(ok=False), scraper=scraper, report=rep)
    assert rep.failed_sends == 1 and "test:z" not in st.entries


def test_health_alert_after_streak(make_cfg):
    cfg = make_cfg()
    cfg.telegram.health_alert_after_failures = 2
    st, tg = _state(cfg), FakeNotifier()
    for _ in range(2):
        update_health(FakeScraper(cfg, [], fail=True), cfg, st, tg)
    assert st.meta["failure_streak"] == 2 and len(tg.messages) == 1
    update_health(FakeScraper(cfg, [make_offer()]), cfg, st, tg)
    assert st.meta["failure_streak"] == 0 and "volvió" in tg.messages[-1]


def test_diversify_and_digest_per_group(make_cfg):
    from tracker.core.pipeline import diversify

    cfg = make_cfg()
    cfg.telegram.max_individual_alerts = 3
    cfg.telegram.digest_max_items = 100
    moda = [make_offer(product_id=f"m{i}", group="Moda", price=5 + i * 0.1) for i in range(40)]
    tec = [make_offer(product_id=f"t{i}", group="Tecnologia", price=10 + i * 0.1) for i in range(5)]
    st, rep, tg = _state(cfg), RunReport(), FakeNotifier()
    scraper = FakeScraper(cfg, moda + tec)
    cands = collect_candidates(scraper, cfg, st, rep)
    elegidas = diversify(cands, 3)
    assert [o.group for o, _ in elegidas] == ["Moda", "Tecnologia", "Moda"]

    notify(cands, cfg=cfg, state=st, notifier=tg, scraper=scraper, report=rep)
    assert rep.sent_individual == 3 and rep.sent_digest == 42 and rep.deferred == 0
    # Moda: 38 restantes → 2 mensajes (máx. 30 por mensaje); Tecnología: 4 → 1 mensaje
    assert rep.digest_messages == 3
    assert sum("Más ofertas · Moda" in m for m in tg.messages) == 2
    assert any("#Test_Tecnologia" in m for m in tg.messages)


# ── Dos fases: la página 1 de todas las categorías sale primero ─────────────────────────────
class PhasedScraper(BaseScraper):
    store_id = "test"
    allowed_seller_ids = frozenset({"1"})

    def __init__(self, cfg, head, rest):
        super().__init__(cfg, {})
        self._head, self._rest = head, rest

    def iter_head(self):
        for o in self._head:
            self.stats.products_seen += 1
            yield o

    def iter_rest(self):
        for o in self._rest:
            self.stats.products_seen += 1
            yield o

    def iter_offers(self):
        yield from self.iter_head()
        yield from self.iter_rest()


class OrderedNotifier:
    """Registra el orden de envíos; `fail` = no acepta nada; `pause_after` = se pausa tras N envíos."""

    def __init__(self, fail=False, pause_after=None):
        self.fail, self.pause_after = fail, pause_after
        self.calls, self.available, self.last_error = [], True, ""

    def _registrar(self, tipo, texto):
        self.calls.append((tipo, texto))
        if self.pause_after is not None and len(self.calls) >= self.pause_after:
            self.available, self.last_error = False, "Telegram pidió esperar 900s (429)."
            return False
        return not self.fail

    def send_offer(self, caption, *, photo_url="", thread_id=None, photo_loader=None):
        return self._registrar("foto", caption)

    def send_message(self, text, *, thread_id=None, silent=False, preview=True):
        return self._registrar("texto", text)


def _ofertas():
    head = [make_offer(product_id="h1", name="Cabecera Uno", price=3, group="Alimentos"),        # 90 %
            make_offer(product_id="h2", name="Cabecera Dos", price=4.5, group="Moda")]          # 85 %
    rest = [make_offer(product_id="r1", name="Profunda Uno", price=1.5, group="Alimentos"),     # 95 %
            make_offer(product_id="r2", name="Profunda Dos", price=12, group="Alimentos"),      # 60 %
            make_offer(product_id="h1", name="Cabecera Uno", price=3, group="Alimentos")]       # repetida
    return head, rest


def test_head_offers_are_sent_before_deeper_pages_are_read(make_cfg):
    from tracker.core.pipeline import run_phases

    cfg = make_cfg()                      # max_individual_alerts=2, digest_max_items=3
    head, rest = _ofertas()
    st, rep, tg = _state(cfg), RunReport(), OrderedNotifier()
    run_phases(PhasedScraper(cfg, head, rest), cfg, st, tg, rep, started_at=0.0)
    tipos = [t for t, _ in tg.calls]
    assert tipos == ["foto", "foto", "texto"]               # 2 fotos de la fase 1 y luego el resumen
    assert "Cabecera Uno" in tg.calls[0][1] and "Cabecera Dos" in tg.calls[1][1]
    assert "Profunda Uno" in tg.calls[2][1] and "Profunda Dos" in tg.calls[2][1]
    assert rep.rejected == {"duplicado": 1} and rep.candidates == 4 and rep.deferred == 0
    assert rep.first_alert_s is not None


def test_unused_photo_slots_go_to_the_second_phase(make_cfg):
    from tracker.core.pipeline import run_phases

    cfg = make_cfg()
    cfg.telegram.max_individual_alerts = 3
    head, rest = _ofertas()
    st, rep, tg = _state(cfg), RunReport(), OrderedNotifier()
    run_phases(PhasedScraper(cfg, head, rest), cfg, st, tg, rep)
    assert [t for t, _ in tg.calls] == ["foto", "foto", "foto", "texto"]
    assert "Profunda Uno" in tg.calls[2][1]                  # el 3er cupo de foto: la mejor de la fase 2
    assert rep.sent_individual == 3 and rep.sent_digest == 1


def test_phase_one_failures_are_not_retried_in_the_same_run(make_cfg):
    from tracker.core.pipeline import run_phases

    cfg = make_cfg()
    head, rest = _ofertas()
    st, rep, tg = _state(cfg), RunReport(), OrderedNotifier(fail=True)
    run_phases(PhasedScraper(cfg, head, rest), cfg, st, tg, rep)
    fotos = [texto for tipo, texto in tg.calls if tipo == "foto"]
    assert len(fotos) == 4 and sum("Cabecera" in f for f in fotos) == 2      # h1 y h2 una sola vez
    assert st.entries == {} and rep.deferred == 4


def test_telegram_pause_stops_sending_and_defers_the_rest(make_cfg):
    from tracker.core.pipeline import run_phases

    cfg = make_cfg()
    head, rest = _ofertas()
    st, rep, tg = _state(cfg), RunReport(), OrderedNotifier(pause_after=1)
    run_phases(PhasedScraper(cfg, head, rest), cfg, st, tg, rep)
    assert len(tg.calls) == 1                                 # tras la pausa no se intenta nada más
    assert rep.failed_sends == 0 and rep.deferred == 4 and st.entries == {}


# ── Re-aviso cuando baja el mejor precio (p. ej. aparece Tarjeta Oh!) ───────────────────────
def test_new_card_price_triggers_a_drop_alert(make_cfg):
    cfg = make_cfg(min_discount_pct=15)
    st = _state(cfg)
    antes = make_offer(product_id="9", list_price=6.4, price=5.4)                       # −15.6 % online
    st.mark_notified(antes.key, 5.4, 15.6, best=5.4)
    ahora = make_offer(product_id="9", list_price=6.4, price=5.4, card_price=4.08, card_label="Tarjeta Oh!")
    rep, tg = RunReport(), FakeNotifier()
    cands = collect_candidates(FakeScraper(cfg, [ahora]), cfg, st, rep)
    assert [(o.product_id, r) for o, r in cands] == [("9", "drop")]
    notify(cands, cfg=cfg, state=st, notifier=tg, scraper=FakeScraper(cfg, []), report=rep)
    assert "Con Tarjeta Oh! ahora S/ 4.08 · antes avisado a S/ 5.40" in tg.offers[0][0]
    assert st.entries["test:9"].best_price == 4.08


def test_old_entries_without_best_price_do_not_cause_a_burst(make_cfg):
    cfg = make_cfg(min_discount_pct=15)
    st = _state(cfg)
    st.mark_notified("test:9", 5.4, 15.6)                     # historial anterior a esta versión
    ahora = make_offer(product_id="9", list_price=6.4, price=5.4, card_price=4.08)
    assert collect_candidates(FakeScraper(cfg, [ahora]), cfg, st, RunReport()) == []


# ── Salud por categoría ─────────────────────────────────────────────────────────────────────
def test_category_without_products_is_reported_once_and_then_recovers(make_cfg):
    cfg = make_cfg()
    cfg.telegram.health_alert_after_failures = 3
    st, tg = _state(cfg), FakeNotifier()

    def corrida(vacias, ok):
        sc = FakeScraper(cfg, [make_offer()])
        list(sc.iter_offers())
        sc.stats.empty_categories, sc.stats.ok_categories = vacias, ok
        update_health(sc, cfg, st, tg)

    for _ in range(4):
        corrida(["Desayunos"], ["Abarrotes"])
    assert len(tg.messages) == 1 and "Desayunos" in tg.messages[0] and "sin productos" in tg.messages[0]
    corrida([], ["Abarrotes", "Desayunos"])
    assert len(tg.messages) == 2 and "volvieron a responder" in tg.messages[1]
    assert st.meta["category_streaks"] == {}


def test_total_failure_does_not_also_send_category_alerts(make_cfg):
    cfg = make_cfg()
    cfg.telegram.health_alert_after_failures = 1
    st, tg = _state(cfg), FakeNotifier()
    sc = FakeScraper(cfg, [], fail=True)
    sc.stats.failed_categories = ["Abarrotes", "Desayunos"]
    update_health(sc, cfg, st, tg)
    assert len(tg.messages) == 1 and "corridas seguidas sin datos" in tg.messages[0]


# ── Corrida completa: monitor externo (healthcheck) ────────────────────────────────────────
def test_full_run_pings_healthcheck_and_saves_state(make_cfg, monkeypatch):
    from tracker.core import pipeline

    cfg = make_cfg()
    cfg.healthcheck.url = "https://hc.example/abc"
    pings = []
    monkeypatch.setattr(pipeline.healthcheck, "ping", lambda url, *, ok, body, timeout_s: pings.append((url, ok, body)))
    monkeypatch.setattr("tracker.scrapers.build_scraper", lambda c, meta: PhasedScraper(c, *_ofertas()))

    class Notificador(OrderedNotifier):
        def __init__(self, *a, **kw):
            super().__init__()

        def close(self):
            pass

    monkeypatch.setattr(pipeline, "TelegramNotifier", Notificador)
    assert pipeline.run(cfg) == 0
    assert pings and pings[0][0] == "https://hc.example/abc" and pings[0][1] is True and "vistos=5" in pings[0][2]
    assert (cfg.base_dir / "state.json").exists()


def test_dry_run_never_pings_healthcheck(make_cfg, monkeypatch):
    from tracker.core import pipeline

    cfg = make_cfg()
    cfg.healthcheck.url = "https://hc.example/abc"
    pings = []
    monkeypatch.setattr(pipeline.healthcheck, "ping", lambda *a, **kw: pings.append(1))
    monkeypatch.setattr("tracker.scrapers.build_scraper", lambda c, meta: PhasedScraper(c, *_ofertas()))
    assert pipeline.run(cfg, dry_run=True) == 0 and pings == []


def test_lost_health_alert_is_retried_on_the_next_run(make_cfg):
    cfg = make_cfg()
    cfg.telegram.health_alert_after_failures = 2
    st, caido, sano = _state(cfg), FakeNotifier(ok=False), FakeNotifier()
    update_health(FakeScraper(cfg, [], fail=True), cfg, st, caido)        # racha 1
    update_health(FakeScraper(cfg, [], fail=True), cfg, st, caido)        # racha 2: el aviso se pierde
    update_health(FakeScraper(cfg, [], fail=True), cfg, st, sano)         # racha 3: se reintenta
    update_health(FakeScraper(cfg, [], fail=True), cfg, st, sano)         # racha 4: ya avisado
    assert len(caido.messages) == 1 and len(sano.messages) == 1 and st.meta["failure_alerted"] is True


def test_old_state_in_a_failure_streak_is_not_alerted_twice(make_cfg):
    cfg = make_cfg()
    cfg.telegram.health_alert_after_failures = 3
    st, tg = _state(cfg), FakeNotifier()
    st.set_meta("failure_streak", 5)                                     # historial de la versión anterior
    update_health(FakeScraper(cfg, [], fail=True), cfg, st, tg)
    assert tg.messages == []
    update_health(FakeScraper(cfg, [make_offer()]), cfg, st, tg)
    assert len(tg.messages) == 1 and "volvió" in tg.messages[0]


# ------------------------------------------------------- diagnóstico en la corrida
def test_run_stops_red_when_telegram_rejects_token(make_cfg, monkeypatch):
    from tracker.core import pipeline

    cfg = make_cfg()
    construido = []
    monkeypatch.setattr("tracker.scrapers.build_scraper", lambda c, meta: construido.append(1))

    class Rechaza(OrderedNotifier):
        def __init__(self, *a, **kw):
            super().__init__()
            self.last_error = "getMe HTTP 401: Unauthorized"

        def check_connection(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(pipeline, "TelegramNotifier", Rechaza)
    assert pipeline.run(cfg) == 1 and construido == []          # no scrapea si no puede avisar


def test_run_logs_filters_invocation_and_simulation(make_cfg, monkeypatch, caplog, tmp_path):
    from tracker.core import pipeline

    resumen = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(resumen))
    monkeypatch.setenv("TELEGRAM_TOKEN", "")
    cfg = make_cfg()
    monkeypatch.setattr("tracker.scrapers.build_scraper", lambda c, meta: PhasedScraper(c, *_ofertas()))
    with caplog.at_level("INFO"):
        assert pipeline.run(cfg, dry_run=True) == 0
    texto = caplog.text
    assert "MODO SIMULACIÓN" in texto and "Token: VACÍO (se esperaba TELEGRAM_TOKEN)" in texto
    assert "[FILTROS] Fase 1 (página 1): 2 productos superaron los filtros" in texto
    assert "[FILTROS] Total: 4 de 5 productos superaron los filtros" in texto
    assert "[TG] Invocando envío a Telegram [SIMULACIÓN, no sale nada]" in texto
    assert "[TG] Respuestas de la API de Telegram en esta corrida: 0 llamadas reales" in texto
    md = resumen.read_text(encoding="utf-8")
    assert "🧪 SIMULACIÓN" in md and "Enviados (foto) (simulado)" in md


def test_run_logs_real_mode_and_secret_source(make_cfg, monkeypatch, caplog):
    from tracker.core import pipeline

    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("CHAT_ID", "-100")
    cfg = make_cfg()
    cfg.telegram.token, cfg.telegram.chat_id = "123:abc", "-100"
    monkeypatch.setattr("tracker.scrapers.build_scraper", lambda c, meta: PhasedScraper(c, *_ofertas()))

    class Notificador(OrderedNotifier):
        def __init__(self, *a, **kw):
            super().__init__()

        def check_connection(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(pipeline, "TelegramNotifier", Notificador)
    with caplog.at_level("INFO"):
        assert pipeline.run(cfg) == 0
    assert "MODO REAL" in caplog.text
    assert "Token: variable TELEGRAM_TOKEN (definida, 7 caracteres) · chat_id: variable CHAT_ID (definida)" in caplog.text
    assert "123:abc" not in caplog.text

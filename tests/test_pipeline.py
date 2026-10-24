"""Pruebas del orquestador con un scraper y un Telegram falsos."""

from __future__ import annotations

from tests.conftest import make_offer
from tracker.base import BaseScraper
from tracker.pipeline import RunReport, collect_candidates, notify, update_health
from tracker.state import StateStore


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

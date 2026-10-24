from __future__ import annotations

from pathlib import Path

import pytest

from tracker.core.config import AppConfig, HttpConfig, OffersConfig, StateConfig, TelegramConfig
from tracker.core.models import Offer

FIXTURES = Path(__file__).parent


@pytest.fixture
def make_cfg(tmp_path):
    def _make(scraper: dict | None = None, **offers) -> AppConfig:
        return AppConfig(
            store_id="test",
            store_name="Tienda Test",
            hashtag_prefix="Test",
            telegram=TelegramConfig(min_interval_s=0, max_individual_alerts=2, digest_max_items=3,
                                    groups={"Alimentos": {"emoji": "🛒", "thread_id": 7}}),
            offers=OffersConfig(**({"min_discount_pct": 40.0} | offers)),
            state=StateConfig(path=str(tmp_path / "state.json"), ttl_days=3, renotify_drop_pct=3),
            http=HttpConfig(retries=1, delay_min_s=0, delay_max_s=0, backoff_base_s=0),
            scraper=scraper or {},
            base_dir=tmp_path,
        )
    return _make


def make_offer(**kw) -> Offer:
    base = dict(store="test", product_id="1", name="Arroz Costeño 5kg", brand="Costeño", seller_id="1",
                seller_name="Plaza Vea", list_price=30.0, price=12.0, url="https://x.pe/p/1",
                image_url="https://x.pe/i.jpg", category="Abarrotes", group="Alimentos", stock=10)
    base.update(kw)
    return Offer(**base)

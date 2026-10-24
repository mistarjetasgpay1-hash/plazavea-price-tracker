"""Scrapers de tiendas. Plaza Vea usa la API Intelligent Search de VTEX."""

from __future__ import annotations

from typing import Any

from ..core.config import AppConfig
from .base import BaseScraper


def build_scraper(cfg: AppConfig, meta: dict[str, Any]) -> BaseScraper:
    from .vtex import PlazaVeaScraper

    return PlazaVeaScraper(cfg, meta)

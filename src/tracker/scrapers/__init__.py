"""Scrapers de tiendas. Plaza Vea usa la API Intelligent Search de VTEX."""

from __future__ import annotations

from typing import Any

from ..core.config import AppConfig
from .base import BaseScraper


def build_scraper(cfg: AppConfig, meta: dict[str, Any]) -> BaseScraper:
    from .vtex import PlazaVeaScraper

    return PlazaVeaScraper(cfg, meta)


def validate_scraper_config(scraper: Any) -> None:
    """Lanza ConfigError si la sección `scraper` tiene errores (claves, tipos, vendedores…)."""
    from .vtex import parse_scraper_config

    parse_scraper_config(scraper)

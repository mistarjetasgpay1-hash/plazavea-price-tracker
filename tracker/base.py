"""Interfaz común de los scrapers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

from .config import AppConfig
from .models import Offer


@dataclass
class ScrapeStats:
    pages_ok: int = 0
    pages_failed: int = 0
    products_seen: int = 0
    errors: list[str] = field(default_factory=list)
    mode: str = "http"

    def fail(self, message: str) -> None:
        self.pages_failed += 1
        if len(self.errors) < 20:
            self.errors.append(message)

    @property
    def total_failure(self) -> bool:
        """La corrida no obtuvo ningún producto y hubo errores: posible bloqueo o cambio de web."""
        return self.products_seen == 0 and self.pages_failed > 0


class BaseScraper(ABC):
    store_id: str = ""
    allowed_seller_ids: frozenset[str] = frozenset()

    def __init__(self, cfg: AppConfig, meta: dict[str, Any]) -> None:
        self.cfg = cfg
        self.meta = meta        # estado persistente propio del scraper (ej. cursores)
        self.meta_changed = False
        self.stats = ScrapeStats()

    @abstractmethod
    def iter_offers(self) -> Iterator[Offer]:
        """Recorre las fuentes configuradas y produce ofertas ya normalizadas."""

    def fetch_image(self, offer: Offer) -> bytes | None:
        """Descarga la imagen si Telegram no pudo obtenerla por URL (opcional)."""
        return None

    def close(self) -> None:
        """Libera recursos (sesiones, navegador)."""

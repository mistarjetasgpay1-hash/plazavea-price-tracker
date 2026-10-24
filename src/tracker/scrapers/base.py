"""Interfaz común de los scrapers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..core.config import AppConfig
from ..core.models import Offer

MAX_ERRORES = 20


@dataclass
class ScrapeStats:
    pages_ok: int = 0
    pages_failed: int = 0
    products_seen: int = 0
    parse_errors: int = 0                                          # productos ignorados por datos inesperados
    errors: list[str] = field(default_factory=list)
    ok_categories: list[str] = field(default_factory=list)         # su página 1 trajo productos
    empty_categories: list[str] = field(default_factory=list)      # su página 1 respondió sin productos
    failed_categories: list[str] = field(default_factory=list)     # su página 1 falló (red, bloqueo…)
    mode: str = "http"

    def fail(self, message: str) -> None:
        self.pages_failed += 1
        self.note(message)

    def note(self, message: str) -> None:
        """Registra un problema sin contarlo como página fallida."""
        if len(self.errors) < MAX_ERRORES:
            self.errors.append(message)

    @property
    def total_failure(self) -> bool:
        """La corrida no obtuvo ningún producto: bloqueo, red caída o un cambio en la web
        (p. ej. una ruta o el filtro de vendedor renombrados: la API responde 200 pero vacía)."""
        return self.products_seen == 0 and (self.pages_ok + self.pages_failed) > 0

    @property
    def categories_without_data(self) -> list[str]:
        return [*self.empty_categories, *self.failed_categories]


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

    def iter_head(self) -> Iterator[Offer]:
        """Fase 1 (opcional): lo más prometedor (p. ej. la página 1 de cada categoría), para
        avisar cuanto antes. Si un scraper no la implementa, todo va en la fase 2."""
        return iter(())

    def iter_rest(self) -> Iterator[Offer]:
        """Fase 2: el resto de la corrida (por defecto, todo `iter_offers`)."""
        return self.iter_offers()

    def fetch_image(self, offer: Offer) -> bytes | None:
        """Descarga la imagen si Telegram no pudo obtenerla por URL (opcional)."""
        return None

    def close(self) -> None:  # noqa: B027 — opcional para los scrapers sin recursos que liberar
        """Libera recursos (sesiones, navegador)."""

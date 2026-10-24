"""Modelo de datos normalizado que comparten scrapers, filtros y notificadores."""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import discount_pct


@dataclass(slots=True)
class Offer:
    """Una oferta ya normalizada, independiente de la tienda de origen."""

    store: str              # id de la tienda: "plazavea" | "falabella"
    product_id: str
    name: str
    brand: str
    seller_id: str          # id del vendedor según la tienda (ej. "1", "FALABELLA_PERU")
    seller_name: str
    list_price: float       # precio regular / tachado
    price: float            # precio de oferta (sin tarjeta, salvo que `note` diga lo contrario)
    url: str
    image_url: str = ""
    category: str = ""      # fuente de la que salió (ej. "Abarrotes")
    group: str = ""         # grupo de alerta para hashtag/ruteo (ej. "Alimentos")
    card_price: float | None = None    # precio exclusivo con tarjeta (CMR, Oh!)
    stock: int | None = None
    min_discount: float | None = None  # umbral propio de la fuente (si lo tiene)
    note: str = ""          # aclaración que se muestra en el mensaje

    @property
    def key(self) -> str:
        """Clave única usada en el historial (caché)."""
        return f"{self.store}:{self.product_id}"

    @property
    def discount(self) -> float:
        return discount_pct(self.list_price, self.price)

    @property
    def savings(self) -> float:
        return max(self.list_price - self.price, 0.0)

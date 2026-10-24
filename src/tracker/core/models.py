"""Modelo de datos normalizado que comparten scrapers, promociones, filtros y Telegram."""

from __future__ import annotations

from dataclasses import dataclass, field

from .pricing import discount_pct


@dataclass(frozen=True, slots=True)
class Quantity:
    """Contenido del producto ya normalizado: kilos, litros o unidades."""

    amount: float
    unit: str            # "kg" | "L" | "un"

    def unit_price(self, price: float) -> float | None:
        return round(price / self.amount, 2) if self.amount > 0 and price > 0 else None


@dataclass(slots=True)
class Promo:
    """Mecánica promocional detectada (combo, 3x2, 2da unidad al X %, precio por cantidad…)."""

    kind: str                          # combo | segunda_unidad | nxm | n_por_precio | llevando_n | precio_fijo
    label: str                         # texto listo para el mensaje
    effective_pct: float | None = None  # ahorro equivalente sobre todo lo que se compra


PAGO_CUALQUIERA = "cualquier_medio"
PAGO_TARJETA = "tarjeta"


@dataclass(slots=True)
class Offer:
    """Una oferta ya normalizada, independiente de la tienda de origen."""

    store: str              # id de la tienda: "plazavea"
    product_id: str
    name: str
    brand: str
    seller_id: str          # id del vendedor según la tienda (ej. "1" = Plaza Vea)
    seller_name: str
    list_price: float       # precio regular
    price: float            # precio online: cualquier medio de pago
    url: str
    image_url: str = ""
    category: str = ""      # fuente de la que salió (ej. "Abarrotes")
    group: str = ""         # grupo de alerta para hashtag/ruteo
    card_price: float | None = None    # precio exclusivo con tarjeta (Tarjeta Oh!)
    card_label: str = "Tarjeta"
    stock: int | None = None
    min_discount: float | None = None  # umbral propio de la categoría
    note: str = ""
    promos: list[Promo] = field(default_factory=list)
    quantity: Quantity | None = None
    tags: tuple[str, ...] = ()   # ruta de categoría + colecciones de la tienda (ej. "plazaVea | Halloween | 19.10")
    qualified_by: str = ""  # online | tarjeta | promo (lo fija el filtro)

    @property
    def key(self) -> str:
        """Clave única usada en el historial (caché)."""
        return f"{self.store}:{self.product_id}"

    # ----------------------------------------------------------- descuentos
    @property
    def discount(self) -> float:
        """Descuento con cualquier medio de pago (precio online vs. regular)."""
        return discount_pct(self.list_price, self.price)

    @property
    def card_discount(self) -> float:
        return discount_pct(self.list_price, self.card_price) if self.card_price else 0.0

    @property
    def promo_discount(self) -> float:
        return max((p.effective_pct or 0.0 for p in self.promos), default=0.0)

    @property
    def best_discount(self) -> float:
        return max(self.discount, self.card_discount, self.promo_discount)

    @property
    def payment(self) -> str:
        return PAGO_TARJETA if self.qualified_by == "tarjeta" else PAGO_CUALQUIERA

    @property
    def headline_discount(self) -> float:
        """El % que encabeza el mensaje: el de la mecánica que hizo calificar la oferta."""
        if self.qualified_by == "tarjeta":
            return self.card_discount
        if self.qualified_by == "promo":
            return self.promo_discount
        return self.discount if self.qualified_by == "online" else self.best_discount

    @property
    def effective_price(self) -> float:
        """Precio que se vigila en el historial: el de tarjeta si solo califica con tarjeta."""
        if self.qualified_by == "tarjeta" and self.card_price:
            return self.card_price
        return self.price

    @property
    def savings(self) -> float:
        return max(self.list_price - self.effective_price, 0.0)

    @property
    def unit_price(self) -> float | None:
        return self.quantity.unit_price(self.effective_price) if self.quantity else None

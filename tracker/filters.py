"""Reglas de negocio que decide si una oferta merece alerta."""

from __future__ import annotations

import unicodedata

from .config import OffersConfig
from .models import Offer


def normalize(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar palabras clave."""
    sin_tildes = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sin_tildes if not unicodedata.combining(c)).lower()


def is_official(offer: Offer, allowed_seller_ids: frozenset[str] | set[str]) -> bool:
    """Filtro estricto: solo vendedores oficiales de la tienda (nunca marketplace)."""
    return offer.seller_id in allowed_seller_ids


def rejection_reason(offer: Offer, cfg: OffersConfig) -> str | None:
    """Devuelve el motivo de descarte o None si la oferta pasa todos los filtros."""
    if offer.price <= 0 or offer.list_price <= 0:
        return "sin_precio"
    if offer.price < cfg.min_price:
        return "precio_minimo"
    if offer.stock is not None and offer.stock <= 0:
        return "sin_stock"
    umbral = offer.min_discount if offer.min_discount is not None else cfg.min_discount_pct
    if offer.discount < umbral:
        return "descuento_bajo"
    if cfg.exclude_keywords:
        nombre = normalize(offer.name)
        if any(normalize(k) in nombre for k in cfg.exclude_keywords):
            return "palabra_excluida"
    return None

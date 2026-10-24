"""Reglas de negocio que deciden si una oferta merece alerta."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from .config import OffersConfig
from .models import Offer


def normalize(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar palabras clave."""
    sin_tildes = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sin_tildes if not unicodedata.combining(c)).lower()


def is_official(offer: Offer, allowed_seller_ids: frozenset[str] | set[str]) -> bool:
    """Filtro estricto: solo vendedores oficiales de la tienda (nunca marketplace)."""
    return offer.seller_id in allowed_seller_ids


@lru_cache(maxsize=64)
def _regex(patron: str) -> re.Pattern:
    return re.compile(patron, re.I)


def excluded_tag(offer: Offer, patrones: list[str]) -> str | None:
    """Devuelve la etiqueta (colección/categoría) que coincide con algún patrón excluido."""
    for patron in patrones:
        for etiqueta in offer.tags:
            if _regex(patron).search(etiqueta):
                return etiqueta
    return None


def threshold_for(offer: Offer, cfg: OffersConfig) -> float:
    return offer.min_discount if offer.min_discount is not None else cfg.min_discount_pct


def rejection_reason(offer: Offer, cfg: OffersConfig) -> str | None:
    """Devuelve el motivo de descarte o None si la oferta califica.

    Si califica, fija `offer.qualified_by` con la mecánica que alcanza el umbral, en este orden:
      online  → el precio con cualquier medio de pago ya llega al umbral
      tarjeta → solo llega con el precio exclusivo de Tarjeta Oh!
      promo   → solo llega con un combo / 3x2 / 2da unidad, etc.
    """
    if offer.price <= 0 or offer.list_price <= 0:
        return "sin_precio"
    if offer.price < cfg.min_price:
        return "precio_minimo"
    if offer.stock is not None and offer.stock <= 0:
        return "sin_stock"
    if cfg.exclude_keywords:
        nombre = normalize(offer.name)
        if any(normalize(k) in nombre for k in cfg.exclude_keywords):
            return "palabra_excluida"
    if cfg.exclude_tags and excluded_tag(offer, cfg.exclude_tags):
        return "coleccion_excluida"
    umbral = threshold_for(offer, cfg)
    if offer.discount >= umbral:
        offer.qualified_by = "online"
    elif offer.card_discount >= umbral:
        offer.qualified_by = "tarjeta"
    elif offer.promo_discount >= umbral:
        offer.qualified_by = "promo"
    else:
        return "descuento_bajo"
    return None

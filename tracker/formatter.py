"""Plantillas HTML de los mensajes de Telegram."""

from __future__ import annotations

import re
import unicodedata
from html import escape

from .models import Offer
from .pricing import discount_pct

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def money(valor: float) -> str:
    return f"S/ {valor:,.2f}"


def hashtag(texto: str) -> str:
    """'PlazaVea Lácteos' -> 'PlazaVea_Lacteos' (conserva mayúsculas, sin tildes ni símbolos)."""
    base = unicodedata.normalize("NFKD", texto or "")
    sin_tildes = "".join(c for c in base if not unicodedata.combining(c))
    limpio = re.sub(r"[^0-9A-Za-z_]+", "_", sin_tildes)
    return re.sub(r"_+", "_", limpio).strip("_") or "Oferta"


def offer_caption(
    offer: Offer,
    *,
    reason: str,
    previous_price: float | None,
    store_name: str,
    hashtag_prefix: str,
    emoji: str = "🔥",
) -> str:
    def construir(nombre: str) -> str:
        lineas = [
            f"{emoji} <b>-{offer.discount:.0f}%</b> · {escape(nombre, quote=False)}",
            f"🏷️ {escape(offer.brand or "Sin marca", quote=False)} · Vendido por <b>{escape(offer.seller_name, quote=False)}</b>",
            f"💰 <s>{money(offer.list_price)}</s> → <b>{money(offer.price)}</b>"
            f" (ahorras {money(offer.savings)})",
        ]
        if offer.note:
            lineas.append(f"ℹ️ {escape(offer.note, quote=False)}")
        if offer.card_price and offer.card_price < offer.price:
            lineas.append(
                f"💳 Con tarjeta: {money(offer.card_price)}"
                f" (-{discount_pct(offer.list_price, offer.card_price):.0f}%)"
            )
        if reason == "drop" and previous_price:
            lineas.append(f"📉 <b>¡Bajó más!</b> Antes avisado a {money(previous_price)}")
        if offer.stock is not None and 0 < offer.stock < 9999:
            lineas.append(f"📦 Stock: {offer.stock} unid.")
        lineas.append(f'🔗 <a href="{escape(offer.url, quote=True)}">Ver en {escape(store_name)}</a>')
        etiquetas = [f"#{hashtag(f'{hashtag_prefix}_{offer.group}')}" if offer.group else f"#{hashtag(hashtag_prefix)}"]
        if offer.category and offer.category != offer.group:
            etiquetas.append(f"#{hashtag(offer.category)}")
        lineas.append(" ".join(etiquetas))
        return "\n".join(lineas)

    texto = construir(offer.name)
    if len(texto) > CAPTION_LIMIT:  # recorta el nombre, nunca las etiquetas HTML
        exceso = len(texto) - CAPTION_LIMIT + 1
        texto = construir(offer.name[: max(20, len(offer.name) - exceso)] + "…")
    return texto[:CAPTION_LIMIT]


def digest_message(
    items: list[tuple[Offer, str]],
    *,
    store_name: str,
    hashtag_prefix: str,
) -> tuple[str, list[tuple[Offer, str]]]:
    """Agrupa ofertas en un solo mensaje. Devuelve (texto, ofertas_incluidas)."""
    cabecera = f"📋 <b>Más ofertas en {escape(store_name)}</b>"
    pie = f"#{hashtag(hashtag_prefix)}_Resumen"
    lineas: list[str] = []
    incluidas: list[tuple[Offer, str]] = []
    largo = len(cabecera) + len(pie) + 20
    for offer, reason in items:
        marca = " 📉" if reason == "drop" else ""
        linea = (
            f"• <b>-{offer.discount:.0f}%</b> "
            f'<a href="{escape(offer.url, quote=True)}">{escape(offer.name[:70], quote=False)}</a>'
            f" — {money(offer.price)}{marca}"
        )
        if largo + len(linea) + 1 > MESSAGE_LIMIT - 50:
            break
        lineas.append(linea)
        incluidas.append((offer, reason))
        largo += len(linea) + 1
    texto = "\n".join([f"{cabecera} ({len(incluidas)})", "", *lineas, "", pie])
    return texto, incluidas


def health_message(store_name: str, streak: int, errors: list[str]) -> str:
    detalle = "\n".join(f"• {escape(e[:150])}" for e in errors[:5]) or "• (sin detalle)"
    return (
        f"⚠️ <b>{escape(store_name)}: {streak} corridas seguidas sin datos</b>\n"
        "Posible bloqueo o cambio en la estructura de la web.\n\n"
        f"{detalle}"
    )


def recovery_message(store_name: str) -> str:
    return f"✅ <b>{escape(store_name)}</b>: el monitoreo volvió a obtener datos."

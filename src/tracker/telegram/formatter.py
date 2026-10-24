"""Plantillas HTML de los mensajes de Telegram."""

from __future__ import annotations

import math
import re
import unicodedata
from html import escape, unescape

from ..core.models import Offer
from ..core.pricing import discount_pct
from ..promotions.units import format_unit_price

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def pct(valor: float) -> int:
    """Porcentaje redondeado hacia abajo, igual que la web de Plaza Vea (14.6 % → 14 %): nunca exagera."""
    return math.floor(valor + 1e-9)


def money(valor: float) -> str:
    return f"S/ {valor:,.2f}"


def hashtag(texto: str) -> str:
    """'PlazaVea Lácteos' -> 'PlazaVea_Lacteos' (conserva mayúsculas, sin tildes ni símbolos)."""
    base = unicodedata.normalize("NFKD", texto or "")
    sin_tildes = "".join(c for c in base if not unicodedata.combining(c))
    limpio = re.sub(r"[^0-9A-Za-z_]+", "_", sin_tildes)
    return re.sub(r"_+", "_", limpio).strip("_") or "Oferta"


def _headline_suffix(offer: Offer) -> str:
    """Aclara de dónde sale el % del titular cuando no es el precio online."""
    if offer.qualified_by == "tarjeta":
        return f" con {escape(offer.card_label, quote=False)}"
    if offer.qualified_by == "promo":
        return " en combo"
    return ""


def _payment_line(offer: Offer) -> str:
    if offer.qualified_by == "tarjeta":
        return f"💳 <b>Exclusivo con {escape(offer.card_label, quote=False)}</b>"
    if offer.qualified_by == "promo":
        return "✅ Cualquier medio de pago · 🎁 precio por promoción"
    return "✅ Cualquier medio de pago"


def offer_caption(
    offer: Offer,
    *,
    reason: str,
    previous_price: float | None,
    store_name: str,
    hashtag_prefix: str,
    emoji: str = "🔥",
    drop_via_card: bool = False,
) -> str:
    marca = escape(offer.brand or "Sin marca", quote=False)
    vendedor = escape(offer.seller_name, quote=False)

    def construir(nombre: str) -> str:
        lineas = [
            f"{emoji} <b>-{pct(offer.headline_discount)}%{_headline_suffix(offer)}</b> · {escape(nombre, quote=False)}",
            f"🏷️ {marca} · Vendido por <b>{vendedor}</b>",
        ]
        if offer.price < offer.list_price:
            lineas.append(f"💰 Regular <s>{money(offer.list_price)}</s> → Online <b>{money(offer.price)}</b>"
                          f" (−{pct(offer.discount)}%)")
        else:
            lineas.append(f"💰 Precio: <b>{money(offer.price)}</b>")
        if offer.card_price and offer.card_price < offer.price:
            lineas.append(f"💳 {escape(offer.card_label, quote=False)}: <b>{money(offer.card_price)}</b>"
                          f" (−{pct(discount_pct(offer.list_price, offer.card_price))}%)")
        for promo in offer.promos[:3]:
            lineas.append(f"🎁 {escape(promo.label, quote=False)}")
        if offer.unit_price and offer.quantity:
            lineas.append(f"⚖️ {format_unit_price(offer.unit_price, offer.quantity.unit)}")
        lineas.append(_payment_line(offer))
        if offer.note:
            lineas.append(f"ℹ️ {escape(offer.note, quote=False)}")
        if reason == "drop" and previous_price:
            if drop_via_card and offer.card_price:
                lineas.append(f"📉 <b>¡Bajó más!</b> Con {escape(offer.card_label, quote=False)} ahora "
                              f"{money(offer.card_price)} · antes avisado a {money(previous_price)}")
            else:
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


DIGEST_MAX_ITEMS = 30          # por mensaje (cada línea usa 2 entidades: negrita + enlace)
_TAG_RE = re.compile(r"<[^>]+>")


def _visible_len(html_text: str) -> int:
    """Telegram cuenta el límite de 4096 sobre el texto visible, no sobre las etiquetas HTML."""
    return len(unescape(_TAG_RE.sub("", html_text)))


def digest_message(
    items: list[tuple[Offer, str]],
    *,
    store_name: str,
    hashtag_prefix: str,
    group: str = "",
    emoji: str = "📋",
) -> tuple[str, list[tuple[Offer, str]]]:
    """Agrupa ofertas (idealmente de un mismo grupo) en un mensaje. Devuelve (texto, incluidas)."""
    titulo = f"Más ofertas · {escape(group)}" if group else f"Más ofertas en {escape(store_name)}"
    cabecera = f"{emoji} <b>{titulo}</b>"
    pie = f"#{hashtag(f'{hashtag_prefix}_{group}')}" if group else f"#{hashtag(hashtag_prefix)}_Resumen"
    lineas: list[str] = []
    incluidas: list[tuple[Offer, str]] = []
    largo = _visible_len(cabecera) + len(pie) + 20
    for offer, reason in items:
        if len(incluidas) >= DIGEST_MAX_ITEMS:
            break
        extras = ""
        if offer.qualified_by == "tarjeta":
            extras += f" 💳 {escape(offer.card_label, quote=False)}"
        if offer.promos:
            extras += " 🎁"
        if offer.unit_price and offer.quantity:
            extras += f" · {format_unit_price(offer.unit_price, offer.quantity.unit)}"
        if reason == "drop":
            extras += " 📉"
        linea = (
            f"• <b>-{pct(offer.headline_discount)}%</b> "
            f'<a href="{escape(offer.url, quote=True)}">{escape(offer.name[:70], quote=False)}</a>'
            f" — {money(offer.effective_price)}{extras}"
        )
        # Límite visible (Telegram) y límite bruto de seguridad para el payload.
        if largo + _visible_len(linea) + 1 > MESSAGE_LIMIT - 100:
            break
        lineas.append(linea)
        incluidas.append((offer, reason))
        largo += _visible_len(linea) + 1
    leyenda = "💳 = solo con tarjeta · 🎁 = combo/promoción"
    texto = "\n".join([f"{cabecera} ({len(incluidas)})", "", *lineas, "", leyenda, pie])
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


def category_health_message(store_name: str, categories: list[str], streak: int,
                            paths: dict[str, str] | None = None) -> str:
    rutas = paths or {}
    detalle = "\n".join(f"• {escape(c)}" + (f" (<code>{escape(rutas[c])}</code>)" if c in rutas else "")
                        for c in categories[:15])
    return (
        f"⚠️ <b>{escape(store_name)}: {len(categories)} categoría(s) sin productos en las últimas {streak} corridas</b>\n"
        "Puede que Plaza Vea haya cambiado la ruta o el filtro de vendedor (revisa <code>path</code> "
        "en el config) o que esa sección no responda.\n\n"
        f"{detalle}"
    )


def category_recovery_message(store_name: str, categories: list[str]) -> str:
    return f"✅ <b>{escape(store_name)}</b>: volvieron a responder: {escape(', '.join(categories[:15]))}."

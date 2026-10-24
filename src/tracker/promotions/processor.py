"""Procesador de promociones de VTEX (Plaza Vea).

Estructuras reales observadas en `commertialOffer.teasers` (24/10/2026):

* Precio con tarjeta:
    name "TARJETA OH - PVEA" / "Promo Oh-Pay"
    conditions.parameters: PaymentMethodId = "210" (Tarjeta Oh!) / "202,208,210" (Oh! Pay)
    effects.parameters:   PromotionalPriceTableItemsDiscount = "1.920"
    → precio tarjeta = precio regular − descuento   (web: Regular 6.00 · Online 4.80 · Tarjeta 4.08)

* Combo "paga X lleva Y":
    name "#PAGAXLLEVAY#[30018201]24/10/26 DSCTO%:22.22 SUCUR:264"
    conditions: SkuIdsList1 = SKUs a comprar, SkuIdsList2 = SKU que se lleva con descuento
    effects:    PercentualDiscountSkusList2 = "22.22"
    → si Y es el mismo producto: "2da unidad al −X %" (100 % = 2x1)

* Propiedad del producto "Descuentos exclusivos": "con Tarjeta Oh!" / "sin descuentos".

Además se reconocen mecánicas escritas en el nombre del teaser o del sello del producto
(clusterHighlights): "3x2", "2 x S/ 10", "2do al 70%", "Lleva 2 a S/ 9.90", y teasers por
cantidad mínima con descuento porcentual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core.models import Promo

PAYMENT_LABELS = {"210": "Tarjeta Oh!", "202": "Oh! Pay", "208": "Oh! Pay"}
OH_EXCLUSIVE_PROP = "Descuentos exclusivos"

NXM = re.compile(r"(?<![\d.,/])([2-9])\s*[xX×]\s*([1-8])(?![\d.,]|\s*(?:ml|g|gr|kg|l|lt|un|und|cm|m)\b)")
N_POR_PRECIO = re.compile(r"(?<![\d.,/])([2-9])\s*[xX×]\s*S/\.?\s*(\d+(?:[.,]\d{1,2})?)", re.I)
SEGUNDA_UNIDAD = re.compile(
    r"(?<![\d.,])([2-9])\s*(?:do|da|°|º|ra|er|ro|to|ta)?\.?\s*(?:unidad|und\.?|producto)?\s*al?\s+(\d{1,3})\s*%", re.I)
LLEVA_A_PRECIO = re.compile(r"\blleva\w*\s+(.{1,40}?)\s+a\s+S/\.?\s*(\d+(?:[.,]\d{1,2})?)", re.I)


@dataclass(frozen=True)
class ComboRef:
    """Combo pendiente de resolver: hay que consultar el SKU que se lleva con descuento."""

    target_skus: tuple[str, ...]
    discount_pct: float
    same_product: bool


@dataclass
class PromoAnalysis:
    card_price: float | None = None
    card_label: str = ""
    card_exclusive_flag: bool = False      # la ficha dice "Descuentos exclusivos: con Tarjeta Oh!"
    promos: list[Promo] = field(default_factory=list)
    combos: list[ComboRef] = field(default_factory=list)


def _params(bloque: dict | None) -> dict[str, str]:
    return {str(p.get("name")): str(p.get("value")) for p in (bloque or {}).get("parameters") or []}


def _num(texto: str) -> float:
    return float(str(texto).replace(",", "."))


def promos_from_text(texto: str, price: float) -> list[Promo]:
    """Mecánicas escritas en texto libre (nombre de teaser, sello del producto)."""
    if not texto or texto.startswith("#"):
        return []
    promos: list[Promo] = []
    m = N_POR_PRECIO.search(texto)
    if m:
        n, total = int(m.group(1)), _num(m.group(2))
        normal = n * price
        pct = round((normal - total) / normal * 100, 1) if normal > total > 0 else None
        promos.append(Promo("n_por_precio", f"{n} unidades x S/ {total:,.2f}", pct))
    else:
        m = NXM.search(texto)
        if m and int(m.group(1)) > int(m.group(2)):
            n, pagas = int(m.group(1)), int(m.group(2))
            promos.append(Promo("nxm", f"{n}x{pagas}: llevas {n} y pagas {pagas}", round((n - pagas) / n * 100, 1)))
    m = SEGUNDA_UNIDAD.search(texto)
    if m and 0 < int(m.group(2)) <= 100:
        n, pct = int(m.group(1)), int(m.group(2))
        ordinal = {2: "2da", 3: "3ra", 4: "4ta"}.get(n, f"{n}ª")
        etiqueta = f"{n}x1" if pct == 100 and n == 2 else f"{ordinal} unidad con −{pct}%"
        promos.append(Promo("segunda_unidad", etiqueta, round(pct / n, 1)))
    m = LLEVA_A_PRECIO.search(texto)
    if m:
        promos.append(Promo("precio_fijo", f"Lleva {m.group(1).strip()} a S/ {_num(m.group(2)):,.2f}", None))
    return promos


def analyze(commertial_offer: dict, product: dict, *, list_price: float, price: float,
            own_sku: str = "") -> PromoAnalysis:
    """Extrae precio con tarjeta, combos y otras mecánicas de una oferta VTEX."""
    res = PromoAnalysis()
    for prop in product.get("properties") or []:
        if prop.get("name") == OH_EXCLUSIVE_PROP and any("oh" in str(v).lower() for v in prop.get("values") or []):
            res.card_exclusive_flag = True

    vistas: set[str] = set()
    for teaser in commertial_offer.get("teasers") or commertial_offer.get("Teasers") or []:
        nombre = str(teaser.get("name") or teaser.get("<Name>k__BackingField") or "")
        if "oculto" in nombre.lower():          # promociones ocultas / de una sola tienda física
            continue
        condiciones = _params(teaser.get("conditions"))
        efectos = _params(teaser.get("effects"))
        minimo = int((teaser.get("conditions") or {}).get("minimumQuantity") or 0)

        # 1) Precio exclusivo por medio de pago (Tarjeta Oh! / Oh! Pay)
        if "PaymentMethodId" in condiciones and "PromotionalPriceTableItemsDiscount" in efectos:
            try:
                rebaja = _num(efectos["PromotionalPriceTableItemsDiscount"])
            except ValueError:
                continue
            base = list_price if list_price > 0 else price
            precio_tarjeta = round(base - rebaja, 2)
            if 0 < precio_tarjeta < price and (res.card_price is None or precio_tarjeta < res.card_price):
                ids = [i.strip() for i in condiciones["PaymentMethodId"].split(",")]
                res.card_price = precio_tarjeta
                res.card_label = "Tarjeta Oh!" if "210" in ids or "oh" in nombre.lower() else \
                    next((PAYMENT_LABELS[i] for i in ids if i in PAYMENT_LABELS), "Tarjeta")
            continue

        # 2) Combo paga X lleva Y (SkuIdsList1 → SkuIdsList2 con % de descuento)
        if "SkuIdsList2" in condiciones and "PercentualDiscountSkusList2" in efectos:
            try:
                pct = _num(efectos["PercentualDiscountSkusList2"])
            except ValueError:
                continue
            objetivo = tuple(s.strip() for s in condiciones["SkuIdsList2"].split(",") if s.strip())
            compra = {s.strip() for s in condiciones.get("SkuIdsList1", "").split(",")}
            mismo = bool(own_sku) and own_sku in objetivo and own_sku in compra
            if pct > 0 and objetivo:
                res.combos.append(ComboRef(objetivo, pct, mismo))
            continue

        # 3) Descuento por cantidad mínima ("llevando 3, −30 %")
        if minimo >= 2 and ("PercentualDiscount" in efectos or "NominalDiscount" in efectos):
            if "PercentualDiscount" in efectos:
                pct = round(_num(efectos["PercentualDiscount"]), 1)
            else:
                pct = round(_num(efectos["NominalDiscount"]) / (minimo * price) * 100, 1) if price else None
            res.promos.append(Promo("llevando_n", f"Llevando {minimo} unidades: −{pct:.0f}%" if pct else
                                    f"Descuento llevando {minimo} unidades", pct))

        # 4) Mecánica escrita en el nombre del teaser
        for promo in promos_from_text(nombre, price):
            if promo.label not in vistas:
                vistas.add(promo.label)
                res.promos.append(promo)

    for sello in product.get("clusterHighlights") or []:
        for promo in promos_from_text(str(sello.get("name") or ""), price):
            if promo.label not in vistas:
                vistas.add(promo.label)
                res.promos.append(promo)
    return res


def combo_promo(ref: ComboRef, *, price: float, target_name: str | None = None,
                target_price: float | None = None) -> Promo:
    """Convierte un combo resuelto (o no) en una Promo con su ahorro equivalente."""
    pct = ref.discount_pct
    if ref.same_product:
        etiqueta = "2x1: lleva 2 y paga 1" if pct >= 99.9 else f"2da unidad con −{pct:.0f}%"
        return Promo("segunda_unidad", etiqueta, round(pct / 2, 1))
    if target_name and target_price:
        precio_y = round(target_price * (1 - pct / 100), 2)
        ahorro = target_price - precio_y
        efectivo = round(ahorro / (price + target_price) * 100, 1) if price + target_price > 0 else None
        if pct >= 99.9:
            etiqueta = f"Compra 1 y lleva GRATIS {target_name}"
        else:
            etiqueta = f"Compra 1 y lleva {target_name} a S/ {precio_y:,.2f} (−{pct:.0f}%)"
        return Promo("combo", etiqueta, efectivo)
    return Promo("combo", f"Compra 1 y lleva otro producto seleccionado con −{pct:.0f}%", None)

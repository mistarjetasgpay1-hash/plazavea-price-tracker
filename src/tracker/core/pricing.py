"""Utilidades de precios: parseo de montos y cálculo de descuento."""

from __future__ import annotations

import re

_NO_NUMERICO = re.compile(r"[^\d.,]")


def parse_price(raw: object) -> float:
    """Convierte un monto en float.

    Acepta números, textos ('S/ 1,699.90', '24,90') y listas (devuelve el menor
    valor positivo, como hace Falabella con rangos de precio).
    """
    if raw is None or isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (list, tuple)):
        valores = [v for v in (parse_price(x) for x in raw) if v > 0]
        return min(valores) if valores else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)

    texto = _NO_NUMERICO.sub("", str(raw))
    if not texto:
        return 0.0

    if "," in texto and "." in texto:
        # El separador que aparece al final es el decimal.
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        cabeza, _, cola = texto.rpartition(",")
        # '24,90' -> decimal ; '1,699' -> separador de miles
        texto = f"{cabeza.replace(',', '')}.{cola}" if len(cola) in (1, 2) else texto.replace(",", "")
    elif texto.count(".") > 1:
        cabeza, _, cola = texto.rpartition(".")
        texto = f"{cabeza.replace('.', '')}.{cola}" if len(cola) in (1, 2) else texto.replace(".", "")

    try:
        return float(texto)
    except ValueError:
        return 0.0


def discount_pct(list_price: float, price: float) -> float:
    """Porcentaje de descuento de `price` respecto a `list_price` (0 si no aplica)."""
    if list_price <= 0 or price <= 0 or price >= list_price:
        return 0.0
    return round((list_price - price) / list_price * 100.0, 2)

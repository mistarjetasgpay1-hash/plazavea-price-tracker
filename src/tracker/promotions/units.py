"""Precio por unidad: extrae el contenido (kg, L, unidades) del nombre o de las propiedades VTEX.

Ejemplos reales de Plaza Vea:
    "Fideo Tallarín MOLITALIA Bolsa 950g"          → 0.95 kg
    "Chocolate BESOS DE MOZA Clásico Caja 9un x 24g" → 0.216 kg
    "Detergente Líquido SKIP Doypack 3L"           → 3 L
    "Papel Higiénico BALANZÉ Azul 40M Paquete 24un" → 24 un
    "Camote Amarillo Procesado x kg" (measurementUnit=kg) → precio ya es por kg
"""

from __future__ import annotations

import re

from ..core.models import Quantity

_NUM = r"(\d+(?:[.,]\d+)?)"
_PESO_VOL = r"(kg|kilos?|kgs|gr|grs|gramos|g|ml|cc|lts?|litros?|l)"
_UNIDADES = r"(?:un|und|unds|unid|unidades|uds|u|rollos|pañales|toallas|sobres|bolsas|piezas)"

MULTIPACK = re.compile(rf"(\d+)\s*{_UNIDADES}?\.?\s*[xX×]\s*{_NUM}\s*{_PESO_VOL}\b", re.I)
TAMANO = re.compile(rf"(?<![\w.,]){_NUM}\s*{_PESO_VOL}\b", re.I)
SOLO_UNIDADES = re.compile(rf"(?<![\w.,])(\d+)\s*{_UNIDADES}\b\.?", re.I)

_FACTOR = {
    "kg": (1.0, "kg"), "kilo": (1.0, "kg"), "kilos": (1.0, "kg"), "kgs": (1.0, "kg"),
    "g": (0.001, "kg"), "gr": (0.001, "kg"), "grs": (0.001, "kg"), "gramos": (0.001, "kg"),
    "l": (1.0, "L"), "lt": (1.0, "L"), "lts": (1.0, "L"), "litro": (1.0, "L"), "litros": (1.0, "L"),
    "ml": (0.001, "L"), "cc": (0.001, "L"),
}


def _num(texto: str) -> float:
    return float(texto.replace(",", "."))


def _from_text(texto: str) -> Quantity | None:
    if not texto:
        return None
    m = MULTIPACK.search(texto)
    if m:
        factor, unidad = _FACTOR[m.group(3).lower()]
        return Quantity(round(int(m.group(1)) * _num(m.group(2)) * factor, 6), unidad)
    m = TAMANO.search(texto)
    if m:
        factor, unidad = _FACTOR[m.group(2).lower()]
        cantidad = _num(m.group(1)) * factor
        return Quantity(round(cantidad, 6), unidad) if cantidad > 0 else None
    m = SOLO_UNIDADES.search(texto)
    if m and int(m.group(1)) > 1:
        return Quantity(float(m.group(1)), "un")
    return None


def parse_quantity(name: str, properties: dict[str, str] | None = None,
                   measurement_unit: str | None = None) -> Quantity | None:
    """Devuelve el contenido normalizado o None si no se puede determinar con seguridad."""
    mu = (measurement_unit or "").strip().lower()
    if mu in ("kg", "l"):  # productos a granel: VTEX ya expresa el precio por kg / litro
        return Quantity(1.0, "kg" if mu == "kg" else "L")

    nombre = name or ""
    # Packs de productos distintos ("Detergente 4kg + Suavizante 900ml"): no hay precio unitario honesto.
    if "+" in nombre and re.match(r"\s*pack\b", nombre, re.I):
        return None
    cantidad = _from_text(nombre)
    if cantidad:
        return cantidad
    props = properties or {}
    for clave in ("Contenido Neto", "Presentación unitarios vitrina", "Presentación/Empaque"):
        cantidad = _from_text(props.get(clave, ""))
        if cantidad:
            return cantidad
    unidades = props.get("Unidades Por Paquete", "")
    if unidades.isdigit() and int(unidades) > 1:
        return Quantity(float(unidades), "un")
    return None


def format_unit_price(value: float, unit: str) -> str:
    etiqueta = {"kg": "kg", "L": "litro", "un": "unidad"}.get(unit, unit)
    return f"S/ {value:,.2f} x {etiqueta}"

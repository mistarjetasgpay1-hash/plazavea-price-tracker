"""Precio por unidad: extrae el contenido (kg, L, unidades) del nombre o de las propiedades VTEX.

Ejemplos reales de Plaza Vea:
    "Fideo Tallarín MOLITALIA Bolsa 950g"          → 0.95 kg
    "Chocolate BESOS DE MOZA Clásico Caja 9un x 24g" → 0.216 kg
    "Detergente Líquido SKIP Doypack 3L"           → 3 L
    "Papel Higiénico BALANZÉ Azul 40M Paquete 24un" → 24 un
    "Filete de Atún REAL Lata 80g Paquete 3un"       → 0.24 kg (3 latas de 80 g)
    "Camote Amarillo Procesado x kg" (measurementUnit=kg) → precio ya es por kg

Si el número de unidades va DESPUÉS del tamaño, solo se multiplica cuando el tamaño va
pegado a un envase que siempre es de una unidad ("Lata 80g Paquete 3un"). En otros casos
es ambiguo ("Caldo MAGGI 100g Paquete 25un" = 100 g en total; "Comino SIBARITA Sobre
21.6g 6un" = 6 sobres de 3.6 g) y se prefiere no mostrar precio unitario antes que uno falso.
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
# Número de unidades escrito después del tamaño: "Paquete 6un", "x 3un", "Bolsa 2un", "6un".
CONTEO_POSTERIOR = re.compile(rf"(?<![\w.,])(\d+)\s*{_UNIDADES}\b", re.I)
# Envases que siempre indican el contenido de UNA unidad, justo antes del tamaño.
ENVASE_UNITARIO = re.compile(
    r"\b(?:lata|latita|botella|botellita|frasco|doypack|pote|vaso|galonera|bid[oó]n|"
    r"tetra\s?pa(?:c)?k|envase|empaque)(?:\s+de)?\s*$", re.I)
PACKS_NOMBRADOS = {"six pack": 6, "sixpack": 6, "duo pack": 2, "dúo pack": 2, "pack doble": 2}

_FACTOR = {
    "kg": (1.0, "kg"), "kilo": (1.0, "kg"), "kilos": (1.0, "kg"), "kgs": (1.0, "kg"),
    "g": (0.001, "kg"), "gr": (0.001, "kg"), "grs": (0.001, "kg"), "gramos": (0.001, "kg"),
    "l": (1.0, "L"), "lt": (1.0, "L"), "lts": (1.0, "L"), "litro": (1.0, "L"), "litros": (1.0, "L"),
    "ml": (0.001, "L"), "cc": (0.001, "L"),
}


def _num(texto: str) -> float:
    return float(texto.replace(",", "."))


def _unidades_del_pack(texto: str, fin_tamano: int) -> int:
    """Unidades del pack: conteo escrito después del tamaño o nombre del pack ("Six Pack")."""
    m = CONTEO_POSTERIOR.search(texto, fin_tamano)
    if m and int(m.group(1)) > 1:
        return int(m.group(1))
    bajo = texto.lower()
    return next((n for frase, n in PACKS_NOMBRADOS.items() if frase in bajo), 1)


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
        if cantidad <= 0:
            return None
        unidades = _unidades_del_pack(texto, m.end())
        if unidades > 1:
            if not ENVASE_UNITARIO.search(texto[: m.start()]):
                return None          # ¿tamaño por unidad o total? ambiguo: sin precio unitario
            cantidad *= unidades
        return Quantity(round(cantidad, 6), unidad)
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
    # Packs de productos distintos ("Detergente 4kg + Suavizante 900ml", "Pack Leche 390g + Fortificante"):
    # no hay precio unitario honesto. Un "+" suelto ("Anticaída+ PANTENE … 52ml") no cuenta.
    if "+" in nombre and (re.match(r"\s*pack\b", nombre, re.I) or len(TAMANO.findall(nombre)) >= 2):
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

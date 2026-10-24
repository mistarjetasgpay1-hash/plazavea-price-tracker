"""Procesador de promociones, precio por unidad, medio de pago y formato — con datos reales de
la API de Plaza Vea (24/10/2026)."""

from __future__ import annotations

import copy

import pytest

from tests.conftest import make_offer
from tracker.core.config import OffersConfig
from tracker.core.filters import rejection_reason
from tracker.core.models import Promo, Quantity
from tracker.promotions.processor import ComboRef, analyze, combo_promo, promos_from_text
from tracker.promotions.units import format_unit_price, parse_quantity
from tracker.telegram import formatter

# ── Recortes reales ─────────────────────────────────────────────────────────────────────────
TEASER_OH = {"name": "TARJETA OH - PVEA",
             "conditions": {"parameters": [{"name": "PaymentMethodId", "value": "210"}], "minimumQuantity": 0},
             "effects": {"parameters": [{"name": "PromotionalPriceTableItemsIds", "value": "10536673"},
                                        {"name": "PromotionalPriceTableItemsDiscount", "value": "1.920"}]},
             "generalValues": []}
TEASER_OH_PAY = copy.deepcopy(TEASER_OH) | {"name": "Promo Oh-Pay"}
TEASER_OH_PAY["conditions"] = {"parameters": [{"name": "PaymentMethodId", "value": "208,202,210"}], "minimumQuantity": 0}
TEASER_OCULTO = copy.deepcopy(TEASER_OH) | {"name": "Promo-Tarjeta-Oh-Lurin-Oculto"}
TEASER_OCULTO["effects"] = {"parameters": [{"name": "PromotionalPriceTableItemsDiscount", "value": "5.00"}]}
DON_VITTORIO = {
    "productId": "20277768", "productName": "Fideos DON VITTORIO Spaghetti Bolsa 950g", "brand": "DON VITTORIO",
    "properties": [{"name": "Descuentos exclusivos", "values": ["con Tarjeta Oh!"]}],
}
CO_DON_VITTORIO = {"Price": 4.8, "ListPrice": 6.0, "PriceWithoutDiscount": 6.0, "AvailableQuantity": 50,
                   "teasers": [TEASER_OH_PAY, TEASER_OH, TEASER_OCULTO]}


def teaser_combo(lista1, lista2, pct):
    return {"name": f"#PAGAXLLEVAY#[30018201]24/10/26 DSCTO%:{pct} SUCUR:264",
            "conditions": {"parameters": [{"name": "SkuIdsList1", "value": lista1},
                                          {"name": "SkuIdsList2", "value": lista2}], "minimumQuantity": 1},
            "effects": {"parameters": [{"name": "PercentualDiscountSkusList1", "value": "0"},
                                       {"name": "PercentualDiscountSkusList2", "value": str(pct)}]},
            "generalValues": []}


# ── Tarjeta Oh! ─────────────────────────────────────────────────────────────────────────────
def test_card_price_from_teaser_matches_website():
    # Web: Regular S/ 6.00 · Online S/ 4.80 · Tarjeta S/ 4.08
    res = analyze(CO_DON_VITTORIO, DON_VITTORIO, list_price=6.0, price=4.8, own_sku="20277768")
    assert res.card_price == 4.08 and res.card_label == "Tarjeta Oh!"
    assert res.card_exclusive_flag                      # "Descuentos exclusivos: con Tarjeta Oh!"
    assert res.combos == [] and res.promos == []        # el teaser "Oculto" se ignora


def test_card_price_for_weighed_products_uses_unit_multiplier():
    # Real: "Pollo Entero Fresco con Menudencia x kg" · unitMultiplier 2.2 · Regular 8.90/kg · Online 5.90/kg
    # Teaser: PromotionalPriceTableItemsDiscount 7.26 (por unidad de 2.2 kg) → web: Tarjeta S/ 5.60 x kg
    teaser = copy.deepcopy(TEASER_OH)
    teaser["effects"]["parameters"][1]["value"] = "7.26000"
    co = {"Price": 5.9, "ListPrice": 8.9, "teasers": [teaser]}
    res = analyze(co, {"productName": "Pollo Entero Fresco con Menudencia x kg"}, list_price=8.9, price=5.9,
                  unit_multiplier=2.2)
    assert res.card_price == 5.60


def test_card_price_ignored_if_not_cheaper():
    co = copy.deepcopy(CO_DON_VITTORIO)
    co["teasers"][1]["effects"]["parameters"][1]["value"] = "0.50"   # 6.00 − 0.50 = 5.50 > 4.80
    co["teasers"] = [co["teasers"][1]]
    assert analyze(co, DON_VITTORIO, list_price=6.0, price=4.8).card_price is None


# ── Combos ──────────────────────────────────────────────────────────────────────────────────
def test_combo_buy_x_get_y_real_case():
    # Mayonesa ALACENA 850g (S/ 17.90) → lleva Aceite PRIMOR 900ml (S/ 9.40) con −43.64 %
    co = {"Price": 17.9, "ListPrice": 19.6, "teasers": [teaser_combo("10725999", "10614795", 43.64)]}
    res = analyze(co, {"productName": "Mayonesa ALACENA Doypack 850g"}, list_price=19.6, price=17.9,
                  own_sku="10725999")
    assert res.combos == [ComboRef(("10614795",), 43.64, False)]
    promo = combo_promo(res.combos[0], price=17.9, target_name="Aceite Vegetal PRIMOR Premium Botella 900ml",
                        target_price=9.4)
    assert promo.kind == "combo" and "a S/ 5.30 (−44%)" in promo.label
    assert promo.effective_pct == pytest.approx(15.0, abs=0.1)     # 4.10 de ahorro sobre 27.30


def test_combo_same_product_is_second_unit_or_2x1():
    assert combo_promo(ComboRef(("1",), 70, True), price=10).label == "2da unidad con −70%"
    assert combo_promo(ComboRef(("1",), 70, True), price=10).effective_pct == 35
    dos_por_uno = combo_promo(ComboRef(("1",), 100, True), price=10)
    assert dos_por_uno.label.startswith("2x1") and dos_por_uno.effective_pct == 50


def test_combo_free_and_unresolved():
    gratis = combo_promo(ComboRef(("9",), 100, False), price=4.6, target_name="Galletas", target_price=3.2)
    assert "GRATIS Galletas" in gratis.label and gratis.effective_pct == pytest.approx(41.0, abs=0.1)
    sin_dato = combo_promo(ComboRef(("9",), 60, False), price=4.6)
    assert sin_dato.effective_pct is None and "−60%" in sin_dato.label


# ── Mecánicas en texto ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("texto, tipo, pct", [
    ("3x2 en detergentes", "nxm", 33.3),
    ("Promo 2x1 shampoo", "nxm", 50.0),
    ("2 x S/ 10 en atún", "n_por_precio", 16.7),        # precio unitario 6.00
    ("2do al 70% en suavizantes", "segunda_unidad", 35.0),
    ("Segunda unidad: 2da al 50%", "segunda_unidad", 25.0),
])
def test_promos_from_text(texto, tipo, pct):
    promos = promos_from_text(texto, price=6.0)
    assert promos and promos[0].kind == tipo and promos[0].effective_pct == pytest.approx(pct, abs=0.1)


def test_text_promos_avoid_false_positives():
    assert promos_from_text("Pack 6x400ml", 5.0) == []
    assert promos_from_text("Vea Days Supermercado marzo | Ver todo", 5.0) == []
    assert promos_from_text("#PAGAXLLEVAY#[29930943]24/10/26 DSCTO%:57.35", 5.0) == []
    lleva = promos_from_text("Compra 2 y lleva 1 Lejía a S/ 9.90", 5.0)
    assert lleva[0].kind == "precio_fijo" and "S/ 9.90" in lleva[0].label


def test_minimum_quantity_teaser_and_highlights():
    co = {"teasers": [{"name": "Llevando 3", "conditions": {"parameters": [], "minimumQuantity": 3},
                       "effects": {"parameters": [{"name": "PercentualDiscount", "value": "30"}]}}]}
    prod = {"clusterHighlights": [{"id": "1", "name": "3x2 en Pañales"}]}
    res = analyze(co, prod, list_price=10, price=10)
    assert [p.kind for p in res.promos] == ["llevando_n", "nxm"]
    assert res.promos[0].effective_pct == 30


# ── Precio por unidad ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("nombre, cantidad, unidad", [
    ("Fideos DON VITTORIO Spaghetti Bolsa 950g", 0.95, "kg"),
    ("Chocolate BESOS DE MOZA Clásico Caja 9un x 24g", 0.216, "kg"),
    ("Detergente Líquido  Bio Enzimas SKIP Doypack 3L", 3, "L"),
    ("Shampoo TÍO NACHO Control Caspa Frasco 415ml", 0.415, "L"),
    ("Papel Higiénico BALANZÉ BASICOS Azul 40M Paquete 24un", 24, "un"),
    ("Galletas Saladitas BELL'S Paquete 6un", 6, "un"),
    ("Yogurt Griego GLORIA Natural Pote 1Kg", 1, "kg"),
    ("Aceite Vegetal PRIMOR Premium Botella 900ml", 0.9, "L"),
    ("Arroz Extra COSTEÑO Bolsa 5Kg", 5, "kg"),
    ("Leche Evaporada GLORIA Pack 6un x 400g", 2.4, "kg"),
])
def test_parse_quantity_real_names(nombre, cantidad, unidad):
    q = parse_quantity(nombre)
    assert q is not None and q.unit == unidad and q.amount == pytest.approx(cantidad)


def test_parse_quantity_edge_cases():
    assert parse_quantity("Pack BOLÍVAR Detergente Cuidado Total Bolsa 4kg + Suavizante 900ml") is None
    assert parse_quantity("Cepillo de Dientes Eléctrico ORAL-B Io2 Caja 1un") is None
    assert parse_quantity("Camote Amarillo Procesado x kg", measurement_unit="kg") == Quantity(1.0, "kg")
    assert parse_quantity("Mixtura Natural", {"Contenido Neto": "150 g"}) == Quantity(0.15, "kg")
    assert parse_quantity("Toallas Higiénicas NOSOTRAS Invisible", {"Unidades Por Paquete": "10"}) == Quantity(10, "un")


def test_unit_price_format():
    assert format_unit_price(5.05, "kg") == "S/ 5.05 x kg"
    assert format_unit_price(1.2, "L") == "S/ 1.20 x litro"


# ── Medio de pago y calificación ────────────────────────────────────────────────────────────
def _offer(**kw):
    base = dict(list_price=6.0, price=4.8, card_price=None, min_discount=15.0)
    return make_offer(**(base | kw))


def test_qualification_online_card_and_promo():
    cfg = OffersConfig(min_discount_pct=15)
    online = _offer()                                         # −20 % online
    assert rejection_reason(online, cfg) is None and online.qualified_by == "online"

    solo_tarjeta = _offer(price=5.4, card_price=4.08, card_label="Tarjeta Oh!")   # online −10 %, tarjeta −32 %
    assert rejection_reason(solo_tarjeta, cfg) is None and solo_tarjeta.qualified_by == "tarjeta"
    assert solo_tarjeta.effective_price == 4.08 and solo_tarjeta.headline_discount == pytest.approx(32, abs=0.1)

    combo = _offer(price=6.0, promos=[Promo("combo", "Compra 1 y lleva…", 15.0)])
    assert rejection_reason(combo, cfg) is None and combo.qualified_by == "promo"

    nada = _offer(price=5.6)
    assert rejection_reason(nada, cfg) == "descuento_bajo"


def test_cleaning_threshold_is_20():
    cfg = OffersConfig(min_discount_pct=15)
    detergente = _offer(list_price=10.0, price=8.2, min_discount=20.0)   # −18 %: no llega
    assert rejection_reason(detergente, cfg) == "descuento_bajo"


def test_caption_shows_card_exclusive_unit_price_and_combo():
    o = _offer(name="Fideos DON VITTORIO Spaghetti Bolsa 950g", price=5.4, card_price=4.08,
               card_label="Tarjeta Oh!", quantity=Quantity(0.95, "kg"),
               promos=[Promo("combo", "Compra 1 y lleva Salsa a S/ 2.00 (−50%)", 12.0)])
    rejection_reason(o, OffersConfig(min_discount_pct=15))
    texto = formatter.offer_caption(o, reason="new", previous_price=None, store_name="Plaza Vea",
                                    hashtag_prefix="PlazaVea", emoji="🛒")
    assert "<b>-32% con Tarjeta Oh!</b>" in texto
    assert "💳 Tarjeta Oh!: <b>S/ 4.08</b>" in texto
    assert "💳 <b>Exclusivo con Tarjeta Oh!</b>" in texto
    assert "⚖️ S/ 4.29 x kg" in texto                       # 4.08 / 0.95
    assert "🎁 Compra 1 y lleva Salsa" in texto


def test_caption_any_payment_method():
    o = _offer(quantity=Quantity(0.95, "kg"))
    rejection_reason(o, OffersConfig(min_discount_pct=15))
    texto = formatter.offer_caption(o, reason="new", previous_price=None, store_name="Plaza Vea",
                                    hashtag_prefix="PlazaVea")
    assert "✅ Cualquier medio de pago" in texto and "⚖️ S/ 5.05 x kg" in texto


def test_digest_marks_card_and_unit_price():
    o = _offer(price=5.4, card_price=4.08, card_label="Tarjeta Oh!", quantity=Quantity(0.95, "kg"))
    rejection_reason(o, OffersConfig(min_discount_pct=15))
    texto, incluidas = formatter.digest_message([(o, "new")], store_name="Plaza Vea", hashtag_prefix="PlazaVea",
                                                group="Abarrotes")
    assert len(incluidas) == 1 and "💳 Tarjeta Oh!" in texto and "S/ 4.29 x kg" in texto and "S/ 4.08" in texto


def test_percentages_round_down_like_the_website():
    # Real: Café NESCAFÉ Kirma 180g · Regular 24.00 · Online 20.50 (web: −14 %) · Tarjeta 17.43 (web: −27 %)
    o = _offer(name="Café Instantáneo NESCAFÉ Kirma Lata 180g", list_price=24.0, price=20.5, card_price=17.43,
               card_label="Tarjeta Oh!", quantity=Quantity(0.18, "kg"))
    assert rejection_reason(o, OffersConfig(min_discount_pct=15)) is None and o.qualified_by == "tarjeta"
    texto = formatter.offer_caption(o, reason="new", previous_price=None, store_name="Plaza Vea",
                                    hashtag_prefix="PlazaVea")
    assert "Online <b>S/ 20.50</b> (−14%)" in texto and "<b>S/ 17.43</b> (−27%)" in texto
    assert "<b>-27% con Tarjeta Oh!</b>" in texto

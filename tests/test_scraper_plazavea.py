"""Pruebas del scraper de Plaza Vea con respuestas reales de la API VTEX (recortadas)."""

from __future__ import annotations

import copy

import pytest

from tracker.core.config import ConfigError
from tracker.core.http_client import HttpError
from tracker.scrapers.vtex import PlazaVeaScraper

SCRAPER_CFG = {
    "api_url": "https://www.plazavea.com.pe/api/io/_v/api/intelligent-search/product_search",
    "sellers": {"plazavea": "1", "oechsle": "oechsle"},
    "page_size": 3,
    "max_pages": 3,
    "categories": [
        {"key": "Limpieza", "path": "category-1/limpieza", "group": "Limpieza"},
        {"key": "BebeInfantil", "path": "category-1/bebe-e-infantil", "group": "BebeInfantil",
         "sellers": ["plazavea"]},
    ],
}


def _vendedor(sid, nombre, precio, lista, stock, default):
    return {"sellerId": sid, "sellerName": nombre, "sellerDefault": default,
            "commertialOffer": {"Price": precio, "ListPrice": lista, "PriceWithoutDiscount": lista,
                                "AvailableQuantity": stock}}


# Recorte de una respuesta real (24/10/2026) de category-1/limpieza/vendido-por/plazavea/vendido-por/oechsle
PRODUCTO_OFICIAL = {
    "productId": "45574", "productName": "Papel Aluminio BELL'S 8m x 30cm Caja 1un", "brand": "BELL'S",
    "linkText": "papel-aluminio-bells-8m-x-30cm-caja-1un", "link": "/papel-aluminio-bells-8m-x-30cm-caja-1un/p",
    "items": [{"itemId": "45686",
               "images": [{"imageUrl": "https://plazavea.vtexassets.com/arquivos/ids/3134839/20145911.jpg"}],
               "sellers": [_vendedor("1", "Plaza Vea", 1.9, 5.9, 10000, True)]}],
}
# Producto que VTEX devuelve aun con la faceta, pero con Promart como vendedor por defecto
PRODUCTO_PROMART = {
    "productId": "999", "productName": "Foco LED", "brand": "X", "linkText": "foco-led",
    "items": [{"itemId": "1000", "images": [],
               "sellers": [_vendedor("1", "Plaza Vea", 49.9, 49.9, 0, False),
                           _vendedor("promart", "Promart", 1.0, 88.0, 5, True)]}],
}
PRODUCTO_OECHSLE = {
    "productId": "777", "productName": "Detergente 4kg", "brand": "Y", "link": "https://www.plazavea.com.pe/det/p",
    "items": [{"itemId": "778", "images": [],
               "sellers": [_vendedor("oechsle", "oechsle", 20.0, 50.0, 3, True)]}],
}


def _cat(url):
    return "bebe" if "bebe" in url else "limpieza"


class FakeHttp:
    def __init__(self, paginas, catalogo=None):
        self.paginas = paginas
        self.catalogo = catalogo or {}
        self.llamadas = []
        self.consultas_sku = []

    def get(self, url, *, params=None, expect="text", **_):
        if "catalog_system" in url:                      # consulta del producto "Y" de un combo
            sku = params["fq"].split(":")[1]
            self.consultas_sku.append(sku)
            return self.catalogo.get(sku, [])
        self.llamadas.append((url, params["page"]))
        respuesta = self.paginas.get((_cat(url), params["page"]))
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta or {"products": [], "recordsFiltered": 0}

    def close(self):
        pass


def _scraper(make_cfg, paginas, **cfg_extra):
    cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | cfg_extra, min_discount_pct=49.5)
    http = FakeHttp(paginas)
    return PlazaVeaScraper(cfg, {}, http=http), http


def test_parse_only_official_sellers(make_cfg):
    paginas = {("limpieza", 1): {"products": [PRODUCTO_OFICIAL, PRODUCTO_PROMART, PRODUCTO_OECHSLE],
                                 "recordsFiltered": 3}}
    scraper, http = _scraper(make_cfg, paginas)
    offers = list(scraper.iter_offers())
    assert [o.product_id for o in offers] == ["45574", "777"]
    oficial = offers[0]
    assert oficial.seller_id == "1" and oficial.price == 1.9 and oficial.list_price == 5.9
    assert oficial.discount == pytest.approx(67.8, abs=0.1)
    assert oficial.url == "https://www.plazavea.com.pe/papel-aluminio-bells-8m-x-30cm-caja-1un/p"
    assert oficial.group == "Limpieza" and oficial.image_url.endswith(".jpg")
    # la URL de búsqueda lleva el filtro de vendedor en servidor
    assert http.llamadas[0][0].endswith("category-1/limpieza/vendido-por/plazavea/vendido-por/oechsle")
    # categoría con vendedores restringidos
    assert any(u.endswith("bebe-e-infantil/vendido-por/plazavea") for u, _ in http.llamadas)


def test_stops_when_discount_falls_below_threshold(make_cfg):
    barato = copy.deepcopy(PRODUCTO_OFICIAL)
    barato["items"][0]["sellers"][0]["commertialOffer"].update(Price=5.0)   # 15 %
    paginas = {("limpieza", 1): {"products": [barato] * 3, "recordsFiltered": 300},
               ("limpieza", 2): {"products": [PRODUCTO_OFICIAL] * 3, "recordsFiltered": 300}}
    scraper, http = _scraper(make_cfg, paginas)
    list(scraper.iter_offers())
    assert ("limpieza", 2) not in [(_cat(u), p) for u, p in http.llamadas]


def test_http_error_is_recorded(make_cfg):
    scraper, _ = _scraper(make_cfg, {("limpieza", 1): HttpError("HTTP 503")})
    assert list(scraper.iter_offers()) == []
    assert scraper.stats.pages_failed == 1 and scraper.stats.total_failure


def test_invalid_category_seller(make_cfg):
    cfg = make_cfg(SCRAPER_CFG | {"categories": [{"key": "X", "path": "p", "sellers": ["promart"]}]})
    with pytest.raises(ConfigError):
        PlazaVeaScraper(cfg, {}, http=FakeHttp({}))


def test_repo_configs_are_valid():
    from pathlib import Path

    from tracker.core.config import load_config

    raiz = Path(__file__).parents[1]
    alim = load_config(raiz / "config.alimentos.yaml", require_secrets=False)
    gen = load_config(raiz / "config.general.yaml", require_secrets=False)
    rutas = {}
    for cfg in (alim, gen):
        scraper = PlazaVeaScraper(cfg, {}, http=FakeHttp({}))
        assert scraper.allowed_seller_ids == {"1", "oechsle"} and scraper.rotating_pages > 0
        for c in scraper.categories:
            assert c.group in cfg.telegram.groups, c.key
            assert c.path not in rutas, f"{c.path} está en ambos bots"
            rutas[c.path] = cfg.label
    assert alim.state.path != gen.state.path                       # historiales separados
    # Bot de canasta básica: bot de Telegram propio y umbrales 15 % / 20 %
    assert alim.telegram.token_env == "TELEGRAM_TOKEN3" and alim.telegram.chat_id_env == "CHAT_ID3"
    umbrales = {c.key: c.min_discount for c in PlazaVeaScraper(alim, {}, http=FakeHttp({})).categories}
    assert umbrales["Abarrotes"] == 15 and umbrales["LacteosHuevos"] == 15 and umbrales["Desayunos"] == 15
    assert umbrales["Limpieza"] == 20 and umbrales["CuidadoPersonal"] == 20 and umbrales["PanalesToallitas"] == 20
    assert {c.group for c in PlazaVeaScraper(gen, {}, http=FakeHttp({})).categories} >= {
        "Tecnologia", "Electro", "Juguetes", "Moda", "Frescos", "Bebidas"}
    assert gen.telegram.token_env[-1] == "TELEGRAM_TOKEN" and gen.telegram.chat_id_env[-1] == "CHAT_ID"
    assert "notificados.json" in alim.state.legacy_paths
    assert alim.offers.exclude_tags and "halloween" in alim.offers.exclude_keywords


def test_entry_scripts_point_to_their_config():
    from pathlib import Path

    raiz = Path(__file__).parents[1]
    for bot in ("alimentos", "general"):
        assert f"config.{bot}.yaml" in (raiz / f"run_{bot}.py").read_text()


def _paginas_altas(n, descuento_bajo_desde=None):
    paginas = {}
    for p in range(1, n + 1):
        prod = copy.deepcopy(PRODUCTO_OFICIAL)
        if descuento_bajo_desde and p >= descuento_bajo_desde:
            prod["items"][0]["sellers"][0]["commertialOffer"].update(Price=5.0)   # 15 %
        paginas[("limpieza", p)] = {"products": [prod] * 3, "recordsFiltered": n * 3}
    return paginas


def test_rotating_window_advances_and_wraps(make_cfg):
    extra = {"fixed_pages": 2, "rotating_pages": 2, "max_pages": 4,
             "categories": [{"key": "Limpieza", "path": "category-1/limpieza", "max_pages": 7}]}
    meta = {}
    vistas = []
    for _ in range(3):
        cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | extra, min_discount_pct=49.5)
        http = FakeHttp(_paginas_altas(9))
        scraper = PlazaVeaScraper(cfg, meta, http=http)
        list(scraper.iter_offers())
        vistas.append([p for _, p in http.llamadas])
    assert vistas == [[1, 2, 3, 4], [1, 2, 5, 6], [1, 2, 7, 3]]   # tope 7 → vuelve a la 3
    assert meta["cursors"]["Limpieza"] == 4


def test_rotation_resets_when_offers_end(make_cfg):
    extra = {"fixed_pages": 2, "rotating_pages": 3, "max_pages": 20}
    meta = {"cursors": {"Limpieza": 5}}
    cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | extra | {"categories": SCRAPER_CFG["categories"][:1]},
                   min_discount_pct=49.5)
    http = FakeHttp(_paginas_altas(10, descuento_bajo_desde=6))
    scraper = PlazaVeaScraper(cfg, meta, http=http)
    list(scraper.iter_offers())
    assert [p for _, p in http.llamadas] == [1, 2, 5, 6]          # la 6 ya está bajo el umbral
    assert meta["cursors"]["Limpieza"] == 3 and scraper.meta_changed



# ── Promociones dentro del scraper ──────────────────────────────────────────────────────────
ATUN = {
    "productId": "20145", "productName": "Filete de Atún en Agua BELL'S Lata 140g", "brand": "BELL'S",
    "link": "/filete-de-atun-en-agua-bells-lata-140g/p",
    "items": [{"itemId": "12200761", "images": [], "measurementUnit": "un", "sellers": [{
        "sellerId": "1", "sellerName": "Plaza Vea", "sellerDefault": True,
        "commertialOffer": {"Price": 4.6, "ListPrice": 4.8, "PriceWithoutDiscount": 4.8, "AvailableQuantity": 30,
                            "teasers": [{"name": "#PAGAXLLEVAY#[30018201]24/10/26 DSCTO%:90 SUCUR:264",
                                         "conditions": {"parameters": [
                                             {"name": "SkuIdsList1", "value": "12200759,12200761"},
                                             {"name": "SkuIdsList2", "value": "1164"}], "minimumQuantity": 1},
                                         "effects": {"parameters": [
                                             {"name": "PercentualDiscountSkusList1", "value": "0"},
                                             {"name": "PercentualDiscountSkusList2", "value": "90"}]}}]}}]}],
}
CATALOGO_GALLETAS = {"1164": [{"productName": "Galletas Saladitas BELL'S Paquete 6un", "items": [{
    "itemId": "1164", "sellers": [{"sellerId": "1", "commertialOffer": {"Price": 3.2, "AvailableQuantity": 9}}]}]}]}


def test_combo_is_resolved_with_catalog_and_cached(make_cfg):
    from tracker.scrapers import vtex

    vtex._SKU_CACHE.clear()
    paginas = {("limpieza", 1): {"products": [ATUN], "recordsFiltered": 1}}
    cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | {"categories": SCRAPER_CFG["categories"][:1]}, min_discount_pct=15)
    http = FakeHttp(paginas, CATALOGO_GALLETAS)
    offer = list(PlazaVeaScraper(cfg, {}, http=http).iter_offers())[0]
    combo = offer.promos[0]
    assert combo.kind == "combo" and "Galletas Saladitas BELL'S Paquete 6un a S/ 0.32 (−90%)" in combo.label
    assert combo.effective_pct == pytest.approx(36.9, abs=0.1)           # 2.88 / 7.80
    assert offer.quantity.amount == pytest.approx(0.14)
    # segunda corrida: el SKU sale de la caché (sin nueva consulta)
    list(PlazaVeaScraper(cfg, {}, http=http).iter_offers())
    assert http.consultas_sku == ["1164"]


def test_combo_lookup_skipped_when_it_cannot_reach_threshold(make_cfg):
    from tracker.scrapers import vtex

    vtex._SKU_CACHE.clear()
    poco = copy.deepcopy(ATUN)
    poco["items"][0]["sellers"][0]["commertialOffer"]["teasers"][0]["effects"]["parameters"][1]["value"] = "10"
    cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | {"categories": SCRAPER_CFG["categories"][:1]}, min_discount_pct=15)
    http = FakeHttp({("limpieza", 1): {"products": [poco], "recordsFiltered": 1}}, CATALOGO_GALLETAS)
    offer = list(PlazaVeaScraper(cfg, {}, http=http).iter_offers())[0]
    assert http.consultas_sku == [] and offer.promos[0].effective_pct is None


def test_adaptive_head_keeps_reading_until_two_empty_pages(make_cfg):
    # páginas 1-3 con ofertas, 4-5 sin ofertas que califiquen → se detiene en la 5
    extra = {"fixed_pages": 20, "tail_pages": 2, "rotating_pages": 0, "max_pages": 20,
             "categories": SCRAPER_CFG["categories"][:1]}
    cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | extra, min_discount_pct=49.5)
    http = FakeHttp(_paginas_altas(10, descuento_bajo_desde=4))
    list(PlazaVeaScraper(cfg, {}, http=http).iter_offers())
    assert [p for _, p in http.llamadas] == [1, 2, 3, 4, 5]


def test_sweep_deep_continues_through_empty_pages(make_cfg):
    extra = {"fixed_pages": 2, "tail_pages": 1, "rotating_pages": 2, "max_pages": 8, "sweep_deep": True,
             "categories": SCRAPER_CFG["categories"][:1]}
    meta = {}
    vistas = []
    for _ in range(3):
        cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | extra, min_discount_pct=49.5)
        http = FakeHttp(_paginas_altas(10, descuento_bajo_desde=1))    # nada califica
        list(PlazaVeaScraper(cfg, meta, http=http).iter_offers())
        vistas.append([p for _, p in http.llamadas])
    # la cabecera para en la 1; el barrido profundo sigue 2→3, 4→5, 6→7 aunque estén vacías
    assert vistas == [[1, 2, 3], [1, 4, 5], [1, 6, 7]]



# ── Exclusión de dulces de Halloween (bot de canasta) ───────────────────────────────────────
CARAMELOS = "/Abarrotes/Galletas y Golosinas/Caramelos y Chupetes/"


def _producto(pid, nombre, colecciones=(), categorias=(CARAMELOS,)):
    prod = copy.deepcopy(PRODUCTO_OFICIAL)
    prod.update(productId=pid, productName=nombre, categories=list(categorias),
                productClusters=[{"id": str(i), "name": c} for i, c in enumerate(colecciones)])
    return prod


def test_canasta_excludes_halloween_and_candy_but_keeps_staples(make_cfg):
    from pathlib import Path

    from tracker.core.config import load_config
    from tracker.core.filters import rejection_reason

    reglas = load_config(Path(__file__).parents[1] / "config.alimentos.yaml", require_secrets=False).offers
    productos = [
        # nombres, categorías y colecciones reales (24/10/2026)
        _producto("1", "Calabaza Momia Grande de Halloween con Dulces ARCOR Paquete"),
        _producto("2", "Caramelos ARCOR Sangrientos Mora Bolsa 312g", ["plazaVea | Halloween | 19.10 al 25.10"]),
        _producto("3", "Gomitas Dulces AMBROSOLI Ambrujos Dientes Bolsa", ["plazaVea | Hallowen | 31.10"],
                  ["/Abarrotes/Galletas y Golosinas/Gomitas y Gomas de Mascar/"]),
        _producto("4", "Bombones de Chocolate CHIN CHIN Ojo de Zombie", ["plazaVea | Especial Halloween | 12.10 al 31.10"],
                  ["/Abarrotes/Chocolatería/Bombones/"]),
        # sin colección de campaña, pero con categoría secundaria de Halloween
        _producto("5", "Galletas COSTA Pack de Terror Bolsa 249.5g", ["Colección FOOD Express"],
                  ["/Abarrotes/Galletas y Golosinas/Galletas Dulces/", "/Abarrotes/Galletas y Golosinas/",
                   "/Abarrotes/", "/O/", "/O/dulces de halloween/"]),
        # golosinas sin nada de Halloween: tampoco son canasta básica
        _producto("6", "Caramelos ARCOR Chicha Morada Bolsa 360g", ["Makro | Arma Tu Fiesta"]),
        _producto("7", "Marshmallows GUANDY Surtidos Bolsa 100g", [],
                  ["/Abarrotes/Galletas y Golosinas/Otras Golosinas/"]),
        # se quedan
        _producto("8", "Arroz Extra COSTEÑO Bolsa 5Kg", [], ["/Abarrotes/Arroz/"]),
        _producto("9", "Galletas Saladas FIELD Soda Paquete 6un", [],
                  ["/Abarrotes/Galletas y Golosinas/Galletas Saladas/"]),
        _producto("10", "Leche Condensada NESTLÉ sabor Caramelo Lata 393g", [],
                  ["/Lácteos y Huevos/Leche/Leche Condensada/"]),
    ]
    paginas = {("limpieza", 1): {"products": productos, "recordsFiltered": len(productos)}}
    scraper, _ = _scraper(make_cfg, paginas, categories=SCRAPER_CFG["categories"][:1])
    motivos = {o.product_id: rejection_reason(o, reglas) for o in scraper.iter_offers()}
    assert motivos == {"1": "palabra_excluida", "2": "coleccion_excluida", "3": "coleccion_excluida",
                       "4": "coleccion_excluida", "5": "coleccion_excluida", "6": "coleccion_excluida",
                       "7": "coleccion_excluida", "8": None, "9": None, "10": None}


def test_general_bot_does_not_exclude_candy():
    from pathlib import Path

    from tracker.core.config import load_config

    gen = load_config(Path(__file__).parents[1] / "config.general.yaml", require_secrets=False)
    assert not gen.offers.exclude_tags and not gen.offers.exclude_keywords


def test_invalid_exclude_pattern_is_a_config_error(tmp_path):
    from tracker.core.config import load_config

    ruta = tmp_path / "c.yaml"
    ruta.write_text("store: {id: t}\noffers: {exclude_tags: ['(sin cerrar']}\nscraper: {a: 1}\n")
    with pytest.raises(ConfigError, match="exclude_tags"):
        load_config(ruta, require_secrets=False)

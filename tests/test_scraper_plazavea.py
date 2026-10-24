"""Pruebas del scraper de Plaza Vea con respuestas reales de la API VTEX (recortadas)."""

from __future__ import annotations

import copy

import pytest

from tracker.config import ConfigError
from tracker.http_client import HttpError
from tracker.scraper import PlazaVeaScraper

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
    def __init__(self, paginas):
        self.paginas = paginas
        self.llamadas = []

    def get(self, url, *, params=None, expect="text", **_):
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


def test_repo_config_is_valid():
    from tracker.config import load_config
    from pathlib import Path

    cfg = load_config(Path(__file__).parents[1] / "config.yaml", require_secrets=False)
    scraper = PlazaVeaScraper(cfg, {}, http=FakeHttp({}))
    assert scraper.allowed_seller_ids == {"1", "oechsle"}
    assert len(scraper.categories) >= 10

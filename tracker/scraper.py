"""Scraper de Plaza Vea (plataforma VTEX · API Intelligent Search).

Filtro de tienda oficial en dos capas:
  1. Servidor: faceta `vendido-por/<valor>` (plazavea, oechsle) en la ruta de búsqueda.
  2. Cliente: solo se acepta la oferta (`commertialOffer`) de un vendedor cuyo
     `sellerId` esté en la lista permitida y tenga stock. VTEX a veces devuelve
     productos con Promart u otro marketplace como vendedor por defecto aunque se
     filtre por faceta; esos se descartan aquí.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator

from .base import BaseScraper
from .config import AppConfig, ConfigError
from .http_client import HttpClient, HttpError
from .models import Offer
from .pricing import discount_pct

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Category:
    key: str
    path: str
    group: str
    sellers: tuple[str, ...] | None = None   # subconjunto de facetas para esta categoría
    min_discount: float | None = None
    max_pages: int | None = None             # tope propio de páginas (si no, el global)


class PlazaVeaScraper(BaseScraper):
    store_id = "plazavea"

    def __init__(self, cfg: AppConfig, meta: dict[str, Any], http: HttpClient | None = None) -> None:
        super().__init__(cfg, meta)
        sc = cfg.scraper
        try:
            self.api_url = str(sc["api_url"]).rstrip("/")
            self.seller_facets: dict[str, str] = {str(k): str(v) for k, v in sc["sellers"].items()}
            categorias = sc["categories"]
        except (KeyError, AttributeError) as exc:
            raise ConfigError(f"scraper: falta la clave {exc}") from exc
        self.site_url = str(sc.get("site_url", "https://www.plazavea.com.pe")).rstrip("/")
        self.page_size = int(sc.get("page_size", 50))
        self.max_pages = int(sc.get("max_pages", 3))
        self.allowed_seller_ids = frozenset(self.seller_facets.values())
        self.categories = [
            Category(
                key=str(c["key"]),
                path=str(c["path"]).strip("/"),
                group=str(c.get("group") or c["key"]),
                sellers=tuple(c["sellers"]) if c.get("sellers") else None,
                min_discount=float(c["min_discount"]) if c.get("min_discount") is not None else None,
                max_pages=int(c["max_pages"]) if c.get("max_pages") else None,
            )
            for c in categorias
        ]
        for cat in self.categories:
            for faceta in cat.sellers or ():
                if faceta not in self.seller_facets:
                    raise ConfigError(f"Categoría {cat.key}: vendedor '{faceta}' no está en scraper.sellers")
        self.http = http or HttpClient(cfg.http, backend="requests", headers={"Accept": "application/json"})

    # ----------------------------------------------------------------- helpers
    def _search_url(self, cat: Category) -> str:
        facetas = cat.sellers or tuple(self.seller_facets)
        filtro = "/".join(f"vendido-por/{f}" for f in facetas)
        return f"{self.api_url}/{cat.path}/{filtro}"

    def _product_url(self, prod: dict) -> str:
        link = str(prod.get("link") or "")
        if link.startswith("http"):
            return link
        if link.startswith("/"):
            return f"{self.site_url}{link}"
        slug = str(prod.get("linkText") or "").strip("/")
        return f"{self.site_url}/{slug}/p" if slug else self.site_url

    @staticmethod
    def _offer_discount(oferta: dict) -> float:
        precio = float(oferta.get("Price") or 0)
        lista = float(oferta.get("ListPrice") or 0)
        if lista <= precio:
            lista = float(oferta.get("PriceWithoutDiscount") or 0)
        return discount_pct(lista, precio)

    def _default_discount(self, prod: dict) -> float:
        """Descuento del vendedor por defecto: es el que VTEX usa para ordenar."""
        for item in prod.get("items") or []:
            vendedores = item.get("sellers") or []
            elegido = next((s for s in vendedores if s.get("sellerDefault")), vendedores[0] if vendedores else None)
            if elegido:
                return self._offer_discount(elegido.get("commertialOffer") or {})
        return 0.0

    def parse_product(self, prod: dict, cat: Category) -> Offer | None:
        """Devuelve la oferta del vendedor oficial con stock, o None."""
        for item in prod.get("items") or []:
            imagenes = item.get("images") or []
            imagen = str(imagenes[0].get("imageUrl") or "").strip() if imagenes else ""
            for vendedor in item.get("sellers") or []:
                seller_id = str(vendedor.get("sellerId") or "")
                if seller_id not in self.allowed_seller_ids:
                    continue
                oferta = vendedor.get("commertialOffer") or {}
                precio = float(oferta.get("Price") or 0)
                stock = int(oferta.get("AvailableQuantity") or 0)
                if precio <= 0 or stock <= 0:
                    continue
                lista = float(oferta.get("ListPrice") or 0)
                if lista <= precio:
                    lista = float(oferta.get("PriceWithoutDiscount") or 0) or lista
                return Offer(
                    store=self.store_id,
                    product_id=str(prod.get("productId") or item.get("itemId") or ""),
                    name=str(prod.get("productName") or "Producto sin nombre").strip(),
                    brand=str(prod.get("brand") or "").strip(),
                    seller_id=seller_id,
                    seller_name=str(vendedor.get("sellerName") or seller_id).strip(),
                    list_price=lista,
                    price=precio,
                    url=self._product_url(prod),
                    image_url=imagen,
                    category=cat.key,
                    group=cat.group,
                    stock=stock,
                    min_discount=cat.min_discount,
                )
        return None

    # --------------------------------------------------------------------- API
    def iter_offers(self) -> Iterator[Offer]:
        for cat in self.categories:
            umbral = cat.min_discount if cat.min_discount is not None else self.cfg.offers.min_discount_pct
            url = self._search_url(cat)
            for pagina in range(1, (cat.max_pages or self.max_pages) + 1):
                try:
                    data = self.http.get(
                        url,
                        params={"page": pagina, "count": self.page_size, "sort": "discount:desc"},
                        expect="json",
                    )
                except HttpError as exc:
                    self.stats.fail(f"{cat.key} pág {pagina}: {exc}")
                    log.warning("[%s] pág %s: %s", cat.key, pagina, exc)
                    break

                productos = data.get("products") or []
                self.stats.pages_ok += 1
                if not productos:
                    break
                max_pagina = 0.0
                oficiales = 0
                for prod in productos:
                    self.stats.products_seen += 1
                    max_pagina = max(max_pagina, self._default_discount(prod))
                    offer = self.parse_product(prod, cat)
                    if offer is not None:
                        oficiales += 1
                        yield offer
                log.info("[%s] pág %s: %s productos | %s oficiales con stock | máx. dto %.0f%%",
                         cat.key, pagina, len(productos), oficiales, max_pagina)

                total = int(data.get("recordsFiltered") or 0)
                if pagina * self.page_size >= total:
                    break
                if max_pagina < umbral:
                    # La búsqueda viene ordenada por descuento: las páginas siguientes serán menores.
                    break

    def close(self) -> None:
        self.http.close()


def build_scraper(cfg: AppConfig, meta: dict[str, Any]) -> BaseScraper:
    return PlazaVeaScraper(cfg, meta)

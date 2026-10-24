"""Scraper de Plaza Vea (plataforma VTEX · API Intelligent Search).

Filtro de tienda oficial en dos capas:
  1. Servidor: faceta `vendido-por/<valor>` (plazavea, oechsle) en la ruta de búsqueda.
  2. Cliente: solo se acepta la oferta (`commertialOffer`) de un vendedor cuyo `sellerId`
     esté en la lista permitida y tenga stock. VTEX a veces devuelve productos con Promart
     u otro marketplace como vendedor por defecto aunque se filtre por faceta: se descartan.

Paginado por categoría (la API viene ordenada por descuento online):
  1. Cabecera adaptativa: páginas 1..`fixed_pages`; se detiene tras `tail_pages` páginas
     seguidas sin ninguna oferta que califique (online, Tarjeta Oh! o combo).
  2. Ventana rotativa: `rotating_pages` más profundas por corrida, hasta `max_pages`, con
     cursor persistente. Con `sweep_deep: true` recorre todo el fondo aunque no haya ofertas
     (útil para combos y precios con tarjeta, que aparecen dispersos).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Iterator

from ..core.config import AppConfig, ConfigError
from ..core.http_client import HttpClient, HttpError
from ..core.models import Offer
from ..promotions.processor import analyze, combo_promo
from ..promotions.units import parse_quantity
from .base import BaseScraper

log = logging.getLogger(__name__)

VTEX_MAX_RESULTS = 2500          # Intelligent Search no pagina más allá de ~2500 productos
_SKU_CACHE: dict[str, tuple[float, dict | None]] = {}   # vive mientras corra el proceso (--loop)
SKU_CACHE_TTL_S = 6 * 3600


@dataclass(frozen=True)
class Category:
    key: str
    path: str
    group: str
    sellers: tuple[str, ...] | None = None   # subconjunto de facetas para esta categoría
    min_discount: float | None = None
    max_pages: int | None = None             # tope propio de páginas
    fixed_pages: int | None = None           # tope propio de la cabecera adaptativa


class PlazaVeaScraper(BaseScraper):
    store_id = "plazavea"

    def __init__(self, cfg: AppConfig, meta: dict[str, Any], http: HttpClient | None = None) -> None:
        super().__init__(cfg, meta)
        sc = cfg.scraper
        try:
            self.api_url = str(sc["api_url"]).rstrip("/")
            self.seller_facets: dict[str, str] = {str(k): str(v) for k, v in sc["sellers"].items()}
            categorias = sc["categories"]
        except (KeyError, AttributeError, TypeError) as exc:
            raise ConfigError(f"scraper: falta la clave {exc}") from exc
        self.site_url = str(sc.get("site_url", "https://www.plazavea.com.pe")).rstrip("/")
        self.page_size = int(sc.get("page_size", 50))
        self.max_pages = int(sc.get("max_pages", 3))
        self.fixed_pages = max(1, int(sc.get("fixed_pages", self.max_pages)))
        self.tail_pages = max(1, int(sc.get("tail_pages", 1)))
        self.rotating_pages = max(0, int(sc.get("rotating_pages", 0)))
        self.sweep_deep = bool(sc.get("sweep_deep", False))
        self.combo_lookups_left = int(sc.get("combo_lookups_per_run", 40))
        self.allowed_seller_ids = frozenset(self.seller_facets.values())
        self.categories = [
            Category(
                key=str(c["key"]),
                path=str(c["path"]).strip("/"),
                group=str(c.get("group") or c["key"]),
                sellers=tuple(c["sellers"]) if c.get("sellers") else None,
                min_discount=float(c["min_discount"]) if c.get("min_discount") is not None else None,
                max_pages=int(c["max_pages"]) if c.get("max_pages") else None,
                fixed_pages=int(c["fixed_pages"]) if c.get("fixed_pages") else None,
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

    def _threshold(self, cat: Category) -> float:
        return cat.min_discount if cat.min_discount is not None else self.cfg.offers.min_discount_pct

    # --------------------------------------------------------- catálogo de SKUs
    def lookup_sku(self, sku: str) -> dict | None:
        """Nombre y precio de un SKU (para describir el producto Y de un combo). Con caché."""
        ahora = time.monotonic()
        guardado = _SKU_CACHE.get(sku)
        if guardado and ahora - guardado[0] < SKU_CACHE_TTL_S:
            return guardado[1]
        if self.combo_lookups_left <= 0:
            return None
        self.combo_lookups_left -= 1
        dato = None
        try:
            productos = self.http.get(f"{self.site_url}/api/catalog_system/pub/products/search",
                                      params={"fq": f"skuId:{sku}"}, expect="json")
        except HttpError as exc:
            log.info("No se pudo consultar el SKU %s del combo: %s", sku, exc)
            productos = None
        for prod in productos or []:
            for item in prod.get("items") or []:
                if str(item.get("itemId")) != str(sku):
                    continue
                for vendedor in item.get("sellers") or []:
                    if str(vendedor.get("sellerId")) not in self.allowed_seller_ids:
                        continue
                    oferta = vendedor.get("commertialOffer") or {}
                    if float(oferta.get("Price") or 0) > 0 and int(oferta.get("AvailableQuantity") or 0) > 0:
                        dato = {"name": str(prod.get("productName") or "").strip(),
                                "price": float(oferta["Price"])}
                        break
        _SKU_CACHE[sku] = (ahora, dato)
        return dato

    # ------------------------------------------------------------------ parseo
    def parse_product(self, prod: dict, cat: Category) -> Offer | None:
        """Devuelve la oferta del vendedor oficial con stock (con promociones), o None."""
        propiedades = {str(p.get("name")): "|".join(map(str, p.get("values") or []))
                       for p in prod.get("properties") or []}
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
                if lista <= 0:
                    lista = precio

                try:
                    multiplicador = float(item.get("unitMultiplier") or 1) or 1.0
                except (TypeError, ValueError):
                    multiplicador = 1.0
                promo = analyze(oferta, prod, list_price=lista, price=precio,
                                own_sku=str(item.get("itemId") or ""), unit_multiplier=multiplicador)
                nombre = str(prod.get("productName") or "Producto sin nombre").strip()
                offer = Offer(
                    store=self.store_id,
                    product_id=str(prod.get("productId") or item.get("itemId") or ""),
                    name=nombre,
                    brand=str(prod.get("brand") or "").strip(),
                    seller_id=seller_id,
                    seller_name=str(vendedor.get("sellerName") or seller_id).strip(),
                    list_price=lista,
                    price=precio,
                    url=self._product_url(prod),
                    image_url=imagen,
                    category=cat.key,
                    group=cat.group,
                    card_price=promo.card_price,
                    card_label=promo.card_label or "Tarjeta",
                    stock=stock,
                    min_discount=cat.min_discount,
                    promos=list(promo.promos),
                    quantity=parse_quantity(nombre, propiedades, item.get("measurementUnit")),
                    tags=tuple(str(c) for c in (prod.get("categories") or [])[:1])
                    + tuple(str(c.get("name") or "") for c in prod.get("productClusters") or []),
                )
                self._resolve_combos(offer, promo.combos, self._threshold(cat))
                return offer
        return None

    def _resolve_combos(self, offer: Offer, combos, umbral: float) -> None:
        for ref in combos:
            if ref.same_product:
                offer.promos.append(combo_promo(ref, price=offer.price))
                continue
            # El ahorro equivalente de un combo X+Y siempre es menor que el % sobre Y:
            # solo vale la pena consultar Y si ese % podría alcanzar el umbral.
            destino = self.lookup_sku(ref.target_skus[0]) if ref.discount_pct >= umbral else None
            offer.promos.append(combo_promo(ref, price=offer.price,
                                            target_name=(destino or {}).get("name"),
                                            target_price=(destino or {}).get("price")))

    # --------------------------------------------------------------------- API
    def _scan_page(self, cat: Category, url: str, pagina: int, salida: list[Offer]) -> tuple[int, int] | None:
        """Lee una página; agrega ofertas a `salida`. Devuelve (ofertas que califican, total) o None."""
        try:
            data = self.http.get(url, params={"page": pagina, "count": self.page_size, "sort": "discount:desc"},
                                 expect="json")
        except HttpError as exc:
            self.stats.fail(f"{cat.key} pág {pagina}: {exc}")
            log.warning("[%s] pág %s: %s", cat.key, pagina, exc)
            return None
        productos = data.get("products") or []
        self.stats.pages_ok += 1
        umbral = self._threshold(cat)
        califican = oficiales = 0
        for prod in productos:
            self.stats.products_seen += 1
            offer = self.parse_product(prod, cat)
            if offer is None:
                continue
            oficiales += 1
            if offer.best_discount >= umbral:
                califican += 1
            salida.append(offer)
        log.info("[%s] pág %s: %s productos | %s oficiales | %s califican (≥%.0f%%)",
                 cat.key, pagina, len(productos), oficiales, califican, umbral)
        return califican, int(data.get("recordsFiltered") or 0)

    def iter_offers(self) -> Iterator[Offer]:
        cursores = self.meta.setdefault("cursors", {})
        for cat in self.categories:
            url = self._search_url(cat)
            tope = cat.max_pages or self.max_pages
            cabecera = min(cat.fixed_pages or self.fixed_pages, tope)
            ofertas: list[Offer] = []

            # 1) Cabecera adaptativa
            total_paginas, ultima, vacias, hay_mas, error = 1, 0, 0, True, False
            for pagina in range(1, cabecera + 1):
                res = self._scan_page(cat, url, pagina, ofertas)
                if res is None:
                    error = True
                    break
                califican, total = res
                ultima = pagina
                total_paginas = max(1, min(math.ceil(total / self.page_size),
                                           VTEX_MAX_RESULTS // self.page_size))
                vacias = 0 if califican else vacias + 1
                if pagina >= total_paginas:
                    hay_mas = False
                    break
                if vacias >= self.tail_pages:
                    hay_mas = self.sweep_deep
                    break
            yield from ofertas
            ofertas.clear()

            # 2) Ventana rotativa en el fondo
            inicio = ultima + 1
            limite = min(tope, total_paginas)
            if error or not hay_mas or self.rotating_pages == 0 or limite < inicio:
                if not error and not self.sweep_deep and cursores.get(cat.key) not in (None, inicio):
                    cursores[cat.key] = inicio
                    self.meta_changed = True
                continue
            pagina = int(cursores.get(cat.key, inicio))
            for _ in range(self.rotating_pages):
                if pagina > limite or pagina < inicio:
                    pagina = inicio
                res = self._scan_page(cat, url, pagina, ofertas)
                if res is None:
                    break
                if res[0] == 0 and not self.sweep_deep:   # se acabaron las ofertas: volver arriba
                    pagina = inicio
                    break
                pagina += 1
            yield from ofertas
            ofertas.clear()
            if cursores.get(cat.key) != pagina:
                cursores[cat.key] = pagina
                self.meta_changed = True

    def close(self) -> None:
        self.http.close()


__all__ = ["PlazaVeaScraper", "Category"]

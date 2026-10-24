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

Dos fases por corrida, para avisar cuanto antes sin hacer peticiones extra:
  * `iter_head()`: la página 1 de cada categoría (la de mayores descuentos).
  * `iter_rest()`: el resto de la cabecera adaptativa y la ventana rotativa.
  `iter_offers()` recorre ambas seguidas.

Un producto con datos inesperados se ignora y un error inesperado en una categoría solo
corta esa categoría: nunca se pierde la corrida completa.
"""

from __future__ import annotations

import difflib
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..core.config import AppConfig, ConfigError
from ..core.http_client import HttpClient, HttpError
from ..core.models import Offer
from ..promotions.processor import analyze, combo_promo
from ..promotions.units import parse_quantity
from .base import BaseScraper

log = logging.getLogger(__name__)

VTEX_MAX_RESULTS = 2500          # Intelligent Search no pagina más allá de 2500 productos (página 51 → HTTP 400)
_SKU_CACHE: dict[str, tuple[float, dict | None]] = {}   # vive mientras corra el proceso (--loop)
SKU_CACHE_TTL_S = 6 * 3600
SKU_CACHE_MAX = 5000

SCRAPER_KEYS = frozenset({
    "api_url", "site_url", "page_size", "sellers", "fixed_pages", "tail_pages", "rotating_pages",
    "max_pages", "sweep_deep", "combo_lookups_per_run", "categories",
})
CATEGORY_KEYS = frozenset({"key", "path", "group", "sellers", "min_discount", "max_pages", "fixed_pages"})


@dataclass(frozen=True)
class Category:
    key: str
    path: str
    group: str
    sellers: tuple[str, ...] | None = None   # subconjunto de facetas para esta categoría
    min_discount: float | None = None
    max_pages: int | None = None             # tope propio de páginas
    fixed_pages: int | None = None           # tope propio de la cabecera adaptativa


@dataclass
class _Avance:
    """Paginado de una categoría durante una corrida (se conserva entre las dos fases)."""

    url: str
    tope: int
    cabecera: int
    total_paginas: int = 1
    ultima: int = 0          # última página de la cabecera leída
    vacias: int = 0          # páginas seguidas sin ofertas que califiquen
    hay_mas: bool = True     # vale la pena la ventana rotativa
    error: bool = False
    terminado: bool = False  # la cabecera ya no sigue


# ───────────────────────────────────────────────────────────── validación de la config
def _sin_claves_desconocidas(datos: dict, permitidas: frozenset[str], donde: str) -> None:
    extra = sorted(str(k) for k in set(datos) - permitidas)
    if extra:
        pistas = []
        for clave in extra:
            parecida = difflib.get_close_matches(clave, sorted(permitidas), n=1)
            pistas.append(f"{clave} (¿quisiste decir {parecida[0]}?)" if parecida else clave)
        raise ConfigError(f"{donde}: claves desconocidas: {', '.join(pistas)}")


def _entero(valor: Any, donde: str, minimo: int) -> int:
    if isinstance(valor, bool):
        raise ConfigError(f"{donde} debe ser un número entero (recibido {valor!r}).")
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        raise ConfigError(f"{donde} debe ser un número entero (recibido {valor!r}).") from None
    if numero < minimo:
        raise ConfigError(f"{donde} debe ser mayor o igual a {minimo}.")
    return numero


def parse_scraper_config(sc: Any) -> tuple[dict[str, str], list[Category]]:
    """Valida la sección `scraper` y devuelve (facetas de vendedor → sellerId, categorías).

    Se usa al cargar la configuración, así un error al editar el YAML en caliente se detecta
    antes de la corrida y el modo continuo sigue con la última configuración válida.
    """
    if not isinstance(sc, dict):
        raise ConfigError("La sección 'scraper' debe ser un diccionario.")
    _sin_claves_desconocidas(sc, SCRAPER_KEYS, "scraper")
    if not str(sc.get("api_url") or "").strip():
        raise ConfigError("Falta scraper.api_url.")
    vendedores = sc.get("sellers")
    if not isinstance(vendedores, dict) or not vendedores:
        raise ConfigError("scraper.sellers debe ser un diccionario {faceta: sellerId} con al menos un vendedor.")
    facetas = {str(k): str(v) for k, v in vendedores.items()}
    for clave, minimo in (("page_size", 1), ("max_pages", 1), ("fixed_pages", 1), ("tail_pages", 1),
                          ("rotating_pages", 0), ("combo_lookups_per_run", 0)):
        if sc.get(clave) is not None:
            _entero(sc[clave], f"scraper.{clave}", minimo)

    crudas = sc.get("categories")
    if not isinstance(crudas, list) or not crudas:
        raise ConfigError("scraper.categories debe ser una lista con al menos una categoría.")
    categorias: list[Category] = []
    for indice, cruda in enumerate(crudas, 1):
        if not isinstance(cruda, dict):
            raise ConfigError(f"scraper.categories: el elemento {indice} debe ser un diccionario.")
        nombre = f"Categoría {cruda.get('key') or indice}"
        _sin_claves_desconocidas(cruda, CATEGORY_KEYS, nombre)
        key = str(cruda.get("key") or "").strip()
        path = str(cruda.get("path") or "").strip().strip("/")
        if not key or not path:
            raise ConfigError(f"{nombre}: faltan 'key' y/o 'path'.")
        if any(c.key == key for c in categorias):
            raise ConfigError(f"Categoría {key} repetida: cada 'key' debe ser única (guarda su propio cursor).")

        sellers = cruda.get("sellers")
        if sellers is not None and (not isinstance(sellers, list) or not sellers):
            raise ConfigError(f"Categoría {key}: 'sellers' debe ser una lista de facetas de scraper.sellers.")
        for faceta in sellers or ():
            if str(faceta) not in facetas:
                raise ConfigError(f"Categoría {key}: vendedor '{faceta}' no está en scraper.sellers")

        min_discount = cruda.get("min_discount")
        if min_discount is not None:
            try:
                min_discount = float(min_discount)
            except (TypeError, ValueError):
                raise ConfigError(f"Categoría {key}: min_discount debe ser un número (recibido {min_discount!r}).") from None
            if not 0 <= min_discount <= 100:
                raise ConfigError(f"Categoría {key}: min_discount debe estar entre 0 y 100.")

        categorias.append(Category(
            key=key,
            path=path,
            group=str(cruda.get("group") or key),
            sellers=tuple(str(s) for s in sellers) if sellers else None,
            min_discount=min_discount,
            max_pages=_entero(cruda["max_pages"], f"Categoría {key}: max_pages", 1)
            if cruda.get("max_pages") is not None else None,
            fixed_pages=_entero(cruda["fixed_pages"], f"Categoría {key}: fixed_pages", 1)
            if cruda.get("fixed_pages") is not None else None,
        ))
    return facetas, categorias


def _id_producto(prod: Any) -> str:
    return str(prod.get("productId") or "?") if isinstance(prod, dict) else "?"


class PlazaVeaScraper(BaseScraper):
    store_id = "plazavea"

    def __init__(self, cfg: AppConfig, meta: dict[str, Any], http: HttpClient | None = None) -> None:
        super().__init__(cfg, meta)
        sc = cfg.scraper
        self.seller_facets, self.categories = parse_scraper_config(sc)
        self.api_url = str(sc["api_url"]).rstrip("/")
        self.site_url = str(sc.get("site_url", "https://www.plazavea.com.pe")).rstrip("/")
        self.page_size = int(sc.get("page_size", 50))
        self.max_pages = int(sc.get("max_pages", 3))
        self.fixed_pages = max(1, int(sc.get("fixed_pages", self.max_pages)))
        self.tail_pages = max(1, int(sc.get("tail_pages", 1)))
        self.rotating_pages = max(0, int(sc.get("rotating_pages", 0)))
        self.sweep_deep = bool(sc.get("sweep_deep", False))
        self.combo_lookups_left = int(sc.get("combo_lookups_per_run", 40))
        self.allowed_seller_ids = frozenset(self.seller_facets.values())
        self.http = http or HttpClient(cfg.http, backend="requests", headers={"Accept": "application/json"})
        self._avance: dict[str, _Avance] = {}

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
        try:
            productos = self.http.get(f"{self.site_url}/api/catalog_system/pub/products/search",
                                      params={"fq": f"skuId:{sku}"}, expect="json")
        except HttpError as exc:
            log.info("No se pudo consultar el SKU %s del combo: %s", sku, exc)
            return None      # error pasajero: no se guarda en caché, se reintenta en otra corrida
        dato = None
        for prod in productos if isinstance(productos, list) else []:
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
        if len(_SKU_CACHE) >= SKU_CACHE_MAX:
            for clave in [k for k, (t, _) in _SKU_CACHE.items() if ahora - t >= SKU_CACHE_TTL_S]:
                del _SKU_CACHE[clave]
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
                    # todas las rutas de categoría (principal y secundarias, p. ej.
                    # "/O/dulces de halloween/") + colecciones de campaña de la tienda
                    tags=tuple(str(c) for c in prod.get("categories") or [])
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
            if not isinstance(data, dict):
                raise HttpError(f"respuesta inesperada ({type(data).__name__}) en {url}")
        except HttpError as exc:
            self.stats.fail(f"{cat.key} pág {pagina}: {exc}")
            log.warning("[%s] pág %s: %s", cat.key, pagina, exc)
            if pagina == 1:
                self.stats.failed_categories.append(cat.key)
            return None
        productos = data.get("products")
        productos = productos if isinstance(productos, list) else []
        self.stats.pages_ok += 1
        if pagina == 1:
            if productos:
                self.stats.ok_categories.append(cat.key)
            else:
                self.stats.empty_categories.append(cat.key)
                aviso = f"{cat.key}: la API respondió sin productos (¿cambió la ruta '{cat.path}' o el filtro de vendedor?)"
                self.stats.note(aviso)
                log.warning("%s", aviso)
        umbral = self._threshold(cat)
        califican = oficiales = 0
        for prod in productos:
            self.stats.products_seen += 1
            try:
                offer = self.parse_product(prod, cat)
            except Exception as exc:  # noqa: BLE001 — un producto raro no debe tumbar la corrida
                self.stats.parse_errors += 1
                self.stats.note(f"{cat.key} pág {pagina}: producto {_id_producto(prod)} ignorado "
                                f"({type(exc).__name__}: {exc})")
                log.warning("[%s] pág %s: producto %s ignorado por datos inesperados: %s",
                            cat.key, pagina, _id_producto(prod), exc)
                continue
            if offer is None:
                continue
            oficiales += 1
            if offer.best_discount >= umbral:
                califican += 1
            salida.append(offer)
        try:
            total = int(data.get("recordsFiltered") or 0)
        except (TypeError, ValueError):
            total = 0
        log.info("[%s] pág %s: %s productos | %s oficiales | %s califican (≥%.0f%%)",
                 cat.key, pagina, len(productos), oficiales, califican, umbral)
        return califican, total

    def _avance_de(self, cat: Category) -> _Avance:
        avance = self._avance.get(cat.key)
        if avance is None:
            tope = cat.max_pages or self.max_pages
            avance = _Avance(url=self._search_url(cat), tope=tope,
                             cabecera=min(cat.fixed_pages or self.fixed_pages, tope))
            self._avance[cat.key] = avance
        return avance

    def _leer_cabecera(self, cat: Category, av: _Avance, salida: list[Offer], hasta: int | None) -> None:
        """Cabecera adaptativa: sigue mientras haya ofertas que califiquen (hasta `hasta` páginas)."""
        limite = av.cabecera if hasta is None else min(av.cabecera, hasta)
        while not av.terminado and av.ultima < limite:
            pagina = av.ultima + 1
            res = self._scan_page(cat, av.url, pagina, salida)
            if res is None:
                av.error = av.terminado = True
                return
            califican, total = res
            av.ultima = pagina
            av.total_paginas = max(1, min(math.ceil(total / self.page_size), VTEX_MAX_RESULTS // self.page_size))
            av.vacias = 0 if califican else av.vacias + 1
            if pagina >= av.total_paginas:
                av.hay_mas, av.terminado = False, True
            elif av.vacias >= self.tail_pages:
                av.hay_mas, av.terminado = self.sweep_deep, True
        if av.ultima >= av.cabecera:
            av.terminado = True

    def _leer_ventana(self, cat: Category, av: _Avance, salida: list[Offer]) -> None:
        """Ventana rotativa en el fondo, con cursor persistente por categoría."""
        cursores = self.meta.setdefault("cursors", {})
        inicio = av.ultima + 1
        limite = min(av.tope, av.total_paginas)
        if av.error or not av.hay_mas or self.rotating_pages == 0 or limite < inicio:
            if not av.error and not self.sweep_deep and cursores.get(cat.key) not in (None, inicio):
                cursores[cat.key] = inicio
                self.meta_changed = True
            return
        pagina = int(cursores.get(cat.key, inicio))
        for _ in range(self.rotating_pages):
            if pagina > limite or pagina < inicio:
                pagina = inicio
            res = self._scan_page(cat, av.url, pagina, salida)
            if res is None:
                break
            if res[0] == 0 and not self.sweep_deep:   # se acabaron las ofertas: volver arriba
                pagina = inicio
                break
            pagina += 1
        if cursores.get(cat.key) != pagina:
            cursores[cat.key] = pagina
            self.meta_changed = True

    def _seguro(self, cat: Category, paso: Callable[..., None], *args: Any) -> None:
        """Ejecuta un paso de una categoría; un error inesperado solo corta esa categoría."""
        try:
            paso(*args)
        except Exception as exc:  # noqa: BLE001
            avance = self._avance.get(cat.key)
            if avance:
                avance.error = avance.terminado = True
            self.stats.fail(f"{cat.key}: error inesperado ({type(exc).__name__}: {exc})")
            log.warning("[%s] error inesperado; se sigue con la siguiente categoría.", cat.key, exc_info=True)

    def iter_head(self) -> Iterator[Offer]:
        self._avance = {}
        for cat in self.categories:
            ofertas: list[Offer] = []
            self._seguro(cat, self._leer_cabecera, cat, self._avance_de(cat), ofertas, 1)
            yield from ofertas

    def iter_rest(self) -> Iterator[Offer]:
        for cat in self.categories:
            avance = self._avance_de(cat)
            ofertas: list[Offer] = []
            self._seguro(cat, self._leer_cabecera, cat, avance, ofertas, None)
            yield from ofertas
            ofertas = []
            self._seguro(cat, self._leer_ventana, cat, avance, ofertas)
            yield from ofertas

    def iter_offers(self) -> Iterator[Offer]:
        yield from self.iter_head()
        yield from self.iter_rest()

    def close(self) -> None:
        self.http.close()


__all__ = ["Category", "PlazaVeaScraper", "parse_scraper_config"]

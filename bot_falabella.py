import json
import os
import re
import time
from pathlib import Path
import requests

# 1. Credenciales exclusivas para el Bot de Falabella (Separado de Plaza Vea)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN2", "").strip()
CHAT_ID = os.getenv("CHAT_ID2", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError(
        "Faltan las variables TELEGRAM_TOKEN2 o CHAT_ID2 en los Secrets de GitHub."
    )

# Archivo de estado exclusivo para Falabella (no toca notificados.json de Plaza Vea)
ARCHIVO_ESTADO = Path("notificados_falabella.json")
UMBRAL_DESCUENTO = 45.0
PAGINAS_A_ESCANEAR = 4
MAX_ALERTAS_POR_EJECUCION = 30

# Categorías de Falabella Perú con su ID y slug exacto
CATEGORIAS = [
    {
        "nombre": "Tecnología",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Tecnologia",
        "cat_id": "cat760706",
        "slug": "Tecnologia",
    },
    {
        "nombre": "Electrohogar",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40512/Electrohogar",
        "cat_id": "cat40512",
        "slug": "Electrohogar",
    },
    {
        "nombre": "Celulares",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Celulares-y-Telefonos",
        "cat_id": "cat760706",
        "slug": "Celulares-y-Telefonos",
    },
    {
        "nombre": "Cómputo",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40496/Computo",
        "cat_id": "cat40496",
        "slug": "Computo",
    },
]


def cargar_notificados() -> set:
    if not ARCHIVO_ESTADO.exists():
        return set()
    try:
        with ARCHIVO_ESTADO.open("r", encoding="utf-8") as f:
            datos = json.load(f)
            return set(datos) if isinstance(datos, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def guardar_notificados(notificados: set) -> None:
    with ARCHIVO_ESTADO.open("w", encoding="utf-8") as f:
        json.dump(sorted(notificados), f, indent=2, ensure_ascii=False)


def post_telegram_con_reintento(
    session: requests.Session, endpoint: str, payload: dict
) -> bool:
    for _ in range(2):
        try:
            resp = session.post(endpoint, json=payload, timeout=10)
            if resp.ok:
                return True
            if resp.status_code == 429:
                espera = min(resp.json().get("parameters", {}).get("retry_after", 5), 15)
                print(f"[RATE LIMIT] Esperando {espera}s solicitados por Telegram...")
                time.sleep(espera + 1)
                continue
            print(f"[ERROR TELEGRAM] HTTP {resp.status_code}: {resp.text}")
            return False
        except requests.RequestException as e:
            print(f"[WARN] Error de red con Telegram: {e}")
            return False
    return False


def enviar_alerta_telegram(
    session: requests.Session, mensaje: str, imagen_url: str = ""
) -> bool:
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    if imagen_url:
        payload_foto = {
            "chat_id": CHAT_ID,
            "photo": imagen_url,
            "caption": mensaje[:1024],
        }
        if post_telegram_con_reintento(session, f"{base_url}/sendPhoto", payload_foto):
            return True
        print("[WARN] Falló envío con foto. Reintentando en solo texto...")

    payload_texto = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "disable_web_page_preview": False,
    }
    return post_telegram_con_reintento(session, f"{base_url}/sendMessage", payload_texto)


def buscar_productos_recursivo(nodo) -> list:
    """Encuentra la lista de productos dentro de cualquier respuesta JSON de Falabella."""
    if isinstance(nodo, list):
        if (
            nodo
            and isinstance(nodo[0], dict)
            and ("productId" in nodo[0] or "skuId" in nodo[0])
            and "prices" in nodo[0]
        ):
            return nodo
        for elemento in nodo:
            hallado = buscar_productos_recursivo(elemento)
            if hallado:
                return hallado
    elif isinstance(nodo, dict):
        for llave in ("results", "products", "data", "searchData", "categoryData"):
            if llave in nodo:
                hallado = buscar_productos_recursivo(nodo[llave])
                if hallado:
                    return hallado
        for valor in nodo.values():
            hallado = buscar_productos_recursivo(valor)
            if hallado:
                return hallado
    return []


def inicializar_contexto_catalyst(session: requests.Session, cat: dict) -> str:
    """
    Visita la página de categoría para obtener las cookies de sesión y descubrir
    el 'catalystBaseUrl' exacto que usa Falabella Perú.
    """
    try:
        resp = session.get(cat["url"], timeout=14)
        if not resp.ok:
            return ""
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            resp.text,
            re.DOTALL,
        )
        if match:
            next_data = json.loads(match.group(1))
            page_props = next_data.get("props", {}).get("pageProps", {})

            # Revisar si hay productos directamente en serverData o page
            productos_directos = buscar_productos_recursivo(page_props)
            if productos_directos:
                cat["_productos_pag1"] = productos_directos

            catalyst_url = page_props.get("catalystBaseUrl", "")
            base_url = page_props.get("baseUrl", "")
            if not hasattr(session, "_debug_impreso"):
                print(
                    f"[DEBUG] catalystBaseUrl='{catalyst_url}' | baseUrl='{base_url}' | "
                    f"serverData keys={list(page_props.get('serverData', {}).keys()) if isinstance(page_props.get('serverData'), dict) else type(page_props.get('serverData')).__name__}"
                )
                session._debug_impreso = True
            return catalyst_url.rstrip("/")
    except Exception as e:
        print(f"[WARN] Error inicializando contexto en {cat['nombre']}: {e}")
    return ""


def obtener_productos_falabella(
    session: requests.Session, cat: dict, pagina: int, catalyst_base: str
) -> list:
    if pagina == 1 and "_productos_pag1" in cat:
        return cat.pop("_productos_pag1")

    # Construir candidatos de endpoints usando el catalystBaseUrl descubierto
    endpoints = []
    if catalyst_base:
        if catalyst_base.startswith("http"):
            endpoints.append(f"{catalyst_base}/s/browse/v1/listing/pe")
            endpoints.append(f"{catalyst_base}/v1/listing/pe")
        else:
            endpoints.append(f"https://www.falabella.com.pe{catalyst_base}/s/browse/v1/listing/pe")
            endpoints.append(f"https://www.falabella.com.pe{catalyst_base}/v1/listing/pe")

    endpoints.extend(
        [
            "https://www.falabella.com.pe/s/browse/v1/listing/pe",
            "https://www.falabella.com.pe/falabella-pe/s/browse/v1/listing/pe",
        ]
    )

    params = {
        "categoryId": cat["cat_id"],
        "categoryName": cat["slug"],
        "page": pagina,
        "sortBy": "derived.price.search.discount,desc",
        "zones": "150101",
        "channel": "web",
    }

    headers_api = {
        "Accept": "application/json, text/plain, */*",
        "Referer": cat["url"],
        "x-Requested-With": "XMLHttpRequest",
    }

    for ep in dict.fromkeys(endpoints):
        try:
            resp = session.get(ep, params=params, headers=headers_api, timeout=10)
            if resp.ok and "application/json" in resp.headers.get("Content-Type", ""):
                productos = buscar_productos_recursivo(resp.json())
                if productos:
                    return productos
            elif pagina == 1:
                print(f"[DEBUG] Endpoint {ep} -> HTTP {resp.status_code}")
        except Exception:
            continue

    return []


def limpiar_precio(valor_str: str) -> float:
    if not valor_str:
        return 0.0
    limpio = re.sub(r"[^\d.]", "", str(valor_str).replace(",", ""))
    try:
        return float(limpio)
    except ValueError:
        return 0.0


def extraer_datos_falabella(prod: dict) -> tuple[float, float, str, str]:
    precios = prod.get("prices", [])
    precios_detectados = []

    for p in precios:
        simbolo_lista = p.get("price", [])
        if not simbolo_lista:
            continue
        monto = limpiar_precio(simbolo_lista[0])
        if monto > 0:
            precios_detectados.append(monto)

    if len(precios_detectados) >= 2:
        precio_lista = max(precios_detectados)
        precio_oferta = min(precios_detectados)
    else:
        precio_lista, precio_oferta = 0.0, 0.0

    url_producto = prod.get("url", "").strip()
    if url_producto and not url_producto.startswith("http"):
        url_producto = f"https://www.falabella.com.pe{url_producto}"
    elif not url_producto:
        prod_id = prod.get("productId", "")
        url_producto = f"https://www.falabella.com.pe/falabella-pe/product/{prod_id}"

    media_urls = prod.get("mediaUrls", [])
    imagen_url = ""
    if media_urls:
        primera_img = media_urls[0]
        imagen_url = (
            primera_img
            if primera_img.startswith("http")
            else f"https://falabella.scene7.com/is/image/FalabellaPE/{primera_img}"
        )

    return precio_lista, precio_oferta, url_producto, imagen_url


def monitorear_ofertas() -> None:
    inicio = time.monotonic()
    notificados = cargar_notificados()
    nuevos_registros = False
    alertas_enviadas = 0

    try:
        with requests.Session() as session:
            session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
                }
            )

            catalyst_base = ""
            for cat in CATEGORIAS:
                if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                    break

                descubierto = inicializar_contexto_catalyst(session, cat)
                if descubierto:
                    catalyst_base = descubierto

                for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
                    if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                        break

                    resultados = obtener_productos_falabella(
                        session, cat, pagina, catalyst_base
                    )
                    print(
                        f"[INFO] {cat['nombre']} (pág {pagina}): "
                        f"{len(resultados)} productos escaneados."
                    )

                    if not resultados:
                        break

                    for prod in resultados:
                        if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                            break

                        prod_id = str(prod.get("productId", "")).strip()
                        if not prod_id:
                            continue

                        precio_lista, precio_oferta, url_prod, img_url = extraer_datos_falabella(prod)

                        if (
                            precio_lista <= 0
                            or precio_oferta <= 0
                            or precio_oferta >= precio_lista
                        ):
                            continue

                        clave_unica = f"{prod_id}|{precio_oferta:.2f}"
                        if clave_unica in notificados:
                            continue

                        descuento = ((precio_lista - precio_oferta) / precio_lista) * 100.0

                        if descuento >= UMBRAL_DESCUENTO:
                            nombre = prod.get("displayName", "Producto sin nombre").strip()
                            marca = prod.get("brand", "Genérico").strip()
                            vendedor = prod.get("sellerName", "Falabella").strip()

                            mensaje = (
                                f"🟢 OFERTA FALABELLA (-{descuento:.1f}%)\n\n"
                                f"Producto: {nombre}\n"
                                f"Marca: {marca}\n"
                                f"Vendedor: {vendedor}\n"
                                f"Precio Normal: S/ {precio_lista:,.2f}\n"
                                f"Precio Oferta: S/ {precio_oferta:,.2f}\n\n"
                                f"Enlace: {url_prod}"
                            )

                            if enviar_alerta_telegram(session, mensaje, img_url):
                                notificados.add(clave_unica)
                                nuevos_registros = True
                                alertas_enviadas += 1
                                print(f"[OK] ({alertas_enviadas}/{MAX_ALERTAS_POR_EJECUCION}) {nombre} (-{descuento:.1f}%)")
                                time.sleep(0.5)
    finally:
        if nuevos_registros:
            guardar_notificados(notificados)
            print(f"[INFO] Se guardaron {alertas_enviadas} ofertas nuevas en {ARCHIVO_ESTADO.name}.")
        else:
            print("[INFO] Sin ofertas nuevas que superen el umbral.")

        duracion = time.monotonic() - inicio
        print(f"[FIN] Ejecución completada en {duracion:.2f} s.")


if __name__ == "__main__":
    monitorear_ofertas()

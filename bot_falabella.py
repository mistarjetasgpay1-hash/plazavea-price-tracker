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

# Archivo de estado exclusivo para Falabella
ARCHIVO_ESTADO = Path("notificados_falabella.json")
UMBRAL_DESCUENTO = 45.0
PAGINAS_A_ESCANEAR = 5
MAX_ALERTAS_POR_EJECUCION = 30

# Categorías de Falabella Perú (con IDs corregidos)
CATEGORIAS = [
    {
        "nombre": "Tecnología",
        "cat_id": "cat760706",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Tecnologia",
    },
    {
        "nombre": "Celulares",
        "cat_id": "cat70057",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat70057/Celulares-y-Telefonos",
    },
    {
        "nombre": "Electrohogar",
        "cat_id": "cat40512",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40512/Electrohogar",
    },
    {
        "nombre": "Cómputo y Laptops",
        "cat_id": "cat40496",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40496/Computo",
    },
    {
        "nombre": "Televisores",
        "cat_id": "cat40554",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40554/Televisores",
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


def construir_urls_imagen(prod: dict) -> list[str]:
    """
    Genera una lista de URLs candidatas para la imagen del producto en formato JPEG,
    evitando duplicar 'falabellaPE/' y probando sufijos '_1' y '_01'.
    """
    urls = []
    media_urls = prod.get("mediaUrls") or []
    if isinstance(media_urls, list):
        for item in media_urls[:2]:
            limpia = str(item).strip().lstrip("/")
            if not limpia:
                continue
            if limpia.startswith("http"):
                urls.append(limpia)
            elif limpia.startswith("falabellaPE/"):
                urls.append(f"https://media.falabella.com.pe/{limpia}/public")
            else:
                urls.append(f"https://media.falabella.com.pe/falabellaPE/{limpia}/public")

    prod_id = str(prod.get("productId") or prod.get("skuId") or "").strip()
    if prod_id:
        urls.append(f"https://media.falabella.com.pe/falabellaPE/{prod_id}_1/public")
        urls.append(f"https://media.falabella.com.pe/falabellaPE/{prod_id}_01/public")

    # Eliminar duplicados manteniendo el orden
    vistas = set()
    resultado = []
    for u in urls:
        if u not in vistas:
            vistas.add(u)
            resultado.append(u)
    return resultado


def descargar_imagen_bytes(session: requests.Session, urls_imagen: list[str]) -> bytes | None:
    """
    Descarga la imagen solicitando estrictamente JPEG o PNG (sin AVIF/WebP)
    para que Telegram no rechace el archivo en /sendPhoto.
    """
    headers_img = {
        "Accept": "image/jpeg,image/png,*/*;q=0.5",
        "Referer": "https://www.falabella.com.pe/",
    }
    for url in urls_imagen:
        try:
            resp = session.get(url, headers=headers_img, timeout=8)
            if resp.ok and resp.content and len(resp.content) > 800:
                content_type = resp.headers.get("Content-Type", "").lower()
                if "avif" not in content_type and "html" not in content_type:
                    return resp.content
        except requests.RequestException:
            continue
    return None


def enviar_alerta_telegram(
    session: requests.Session, mensaje: str, urls_imagen: list[str]
) -> bool:
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    # 1. Descargar imagen en JPEG/PNG y subirla como binario a Telegram
    img_bytes = descargar_imagen_bytes(session, urls_imagen)
    if img_bytes:
        for _ in range(2):
            try:
                resp = session.post(
                    f"{base_url}/sendPhoto",
                    data={"chat_id": CHAT_ID, "caption": mensaje[:1024]},
                    files={"photo": ("oferta.jpg", img_bytes, "image/jpeg")},
                    timeout=12,
                )
                if resp.ok:
                    return True
                if resp.status_code == 429:
                    espera = min(resp.json().get("parameters", {}).get("retry_after", 5), 15)
                    time.sleep(espera + 1)
                    continue
                print(f"[WARN] Telegram sendPhoto devolvió {resp.status_code}: {resp.text}")
                break
            except requests.RequestException:
                break

    # 2. Respaldo en texto plano si el producto realmente no tiene foto en el CDN
    payload_texto = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "disable_web_page_preview": False,
    }
    for _ in range(2):
        try:
            resp = session.post(f"{base_url}/sendMessage", json=payload_texto, timeout=10)
            if resp.ok:
                return True
            if resp.status_code == 429:
                espera = min(resp.json().get("parameters", {}).get("retry_after", 5), 15)
                time.sleep(espera + 1)
                continue
            return False
        except requests.RequestException:
            return False
    return False


def buscar_productos_recursivo(nodo) -> list:
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
        for llave in ("results", "products", "data", "searchData", "pageProps"):
            if llave in nodo:
                hallado = buscar_productos_recursivo(nodo[llave])
                if hallado:
                    return hallado
        for valor in nodo.values():
            hallado = buscar_productos_recursivo(valor)
            if hallado:
                return hallado
    return []


def obtener_productos_pagina(
    session: requests.Session, cat: dict, pagina: int
) -> list:
    """
    Intenta primero la API JSON interna de Falabella con filtro de vendedor oficial,
    y si no devuelve resultados usa el HTML (__NEXT_DATA__) como respaldo.
    """
    api_url = "https://www.falabella.com.pe/s/browse/v1/listing/pe"
    variantes_params = [
        {
            "categoryId": cat["cat_id"],
            "page": pagina,
            "sortBy": "derived.price.search.discount,desc",
            "facetSelected": "true",
            "f.derived.variant.sellerId": "FALABELLA",
        },
        {
            "categoryId": cat["cat_id"],
            "page": pagina,
            "sortBy": "derived.price.search.discount,desc",
        },
    ]

    # 1. Intentar por API JSON de Falabella
    for params in variantes_params:
        try:
            resp = session.get(
                api_url,
                params=params,
                headers={"Accept": "application/json, text/plain, */*"},
                timeout=12,
            )
            if resp.ok:
                datos = resp.json()
                productos = buscar_productos_recursivo(datos)
                if productos:
                    return productos
        except Exception:
            pass

    # 2. Respaldo por HTML (__NEXT_DATA__)
    for params_html in (
        {
            "page": pagina,
            "sortBy": "derived.price.search.discount,desc",
            "facetSelected": "true",
            "f.derived.variant.sellerId": "FALABELLA",
        },
        {
            "page": pagina,
            "sortBy": "derived.price.search.discount,desc",
        },
    ):
        try:
            resp = session.get(cat["url"], params=params_html, timeout=14)
            if resp.ok:
                match = re.search(
                    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                    resp.text,
                    re.DOTALL,
                )
                if match:
                    next_data = json.loads(match.group(1))
                    productos = buscar_productos_recursivo(next_data)
                    if productos:
                        return productos
        except Exception as e:
            print(f"[WARN] Error consultando {cat['nombre']} (pág {pagina}): {e}")

    return []


def es_vendedor_oficial(prod: dict) -> bool:
    """
    Verifica estrictamente que el producto sea vendido por Falabella Oficial:
    1. Descarta cualquier producto de Marketplace (IDs de 9 dígitos que inician con '1' o sellerId 'SC...').
    2. Exige que sellerId o sellerName confirmen explícitamente a Falabella.
    """
    prod_id = str(prod.get("productId") or prod.get("skuId") or "").strip()

    # En Falabella Perú, los productos de Marketplace tienen IDs de 9 dígitos que empiezan con 1
    if len(prod_id) >= 9 and prod_id.startswith("1"):
        return False

    seller_name = str(prod.get("sellerName") or "").strip().upper()
    seller_id = str(prod.get("sellerId") or "").strip().upper()

    # Descartar códigos de Seller Center (Marketplace) o tiendas asociadas si no son Falabella
    if seller_id.startswith("SC") or seller_id.startswith("MKP"):
        return False

    vendedores_oficiales = {
        "FALABELLA",
        "FALABELLA PERU",
        "FALABELLA PERÚ",
        "FALABELLA.COM",
        "SAGA FALABELLA",
    }

    # Si trae información de vendedor, debe coincidir exactamente con Falabella Oficial
    if seller_id or seller_name:
        if seller_id not in vendedores_oficiales and seller_name not in vendedores_oficiales:
            return False
    else:
        # Si ambos campos vienen vacíos, nunca asumir True a ciegas
        return False

    nombre = str(prod.get("displayName") or "").upper()
    if "REACONDICIONADO" in nombre or "REFURBISHED" in nombre:
        return False

    return True


def limpiar_precio(valor_raw) -> float:
    if isinstance(valor_raw, list):
        valor_raw = valor_raw[0] if valor_raw else ""
    if not valor_raw:
        return 0.0
    limpio = re.sub(r"[^\d.]", "", str(valor_raw).replace(",", ""))
    try:
        return float(limpio)
    except ValueError:
        return 0.0


def extraer_datos_falabella(prod: dict) -> tuple[float, float, str, list[str]]:
    precios = prod.get("prices", [])
    precios_detectados = []

    for p in precios:
        if not isinstance(p, dict):
            continue
        monto = limpiar_precio(p.get("price"))
        if monto > 0:
            precios_detectados.append(monto)

    if len(precios_detectados) >= 2:
        precio_lista = max(precios_detectados)
        precio_oferta = min(precios_detectados)
    else:
        precio_lista, precio_oferta = 0.0, 0.0

    url_producto = str(prod.get("url") or "").strip()
    if url_producto and not url_producto.startswith("http"):
        url_producto = f"https://www.falabella.com.pe{url_producto}"
    elif not url_producto:
        prod_id = prod.get("productId", "")
        url_producto = f"https://www.falabella.com.pe/falabella-pe/product/{prod_id}"

    urls_imagen = construir_urls_imagen(prod)

    return precio_lista, precio_oferta, url_producto, urls_imagen


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

            for cat in CATEGORIAS:
                if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                    break

                for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
                    if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                        break

                    resultados = obtener_productos_pagina(session, cat, pagina)
                    oficiales = [p for p in resultados if es_vendedor_oficial(p)]

                    print(
                        f"[INFO] {cat['nombre']} (pág {pagina}): "
                        f"{len(resultados)} totales | {len(oficiales)} oficiales de Falabella."
                    )

                    if not resultados:
                        break

                    for prod in oficiales:
                        if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                            break

                        prod_id = str(prod.get("productId", "")).strip()
                        if not prod_id:
                            continue

                        precio_lista, precio_oferta, url_prod, urls_img = extraer_datos_falabella(prod)

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
                            vendedor_real = prod.get("sellerName") or prod.get("sellerId") or "Falabella"

                            mensaje = (
                                f"🟢 OFERTA FALABELLA OFICIAL (-{descuento:.1f}%)\n\n"
                                f"Producto: {nombre}\n"
                                f"Marca: {marca}\n"
                                f"Vendedor: {vendedor_real}\n"
                                f"Precio Normal: S/ {precio_lista:,.2f}\n"
                                f"Precio Oferta: S/ {precio_oferta:,.2f}\n\n"
                                f"Enlace: {url_prod}"
                            )

                            if enviar_alerta_telegram(session, mensaje, urls_img):
                                notificados.add(clave_unica)
                                nuevos_registros = True
                                alertas_enviadas += 1
                                print(
                                    f"[OK] ({alertas_enviadas}/{MAX_ALERTAS_POR_EJECUCION}) "
                                    f"{nombre} (-{descuento:.1f}%)"
                                )
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

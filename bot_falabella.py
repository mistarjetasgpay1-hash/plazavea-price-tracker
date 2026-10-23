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

# Archivo de estado exclusivo para Falabella (no interfiere con notificados.json de Plaza Vea)
ARCHIVO_ESTADO = Path("notificados_falabella.json")
UMBRAL_DESCUENTO = 45.0
PAGINAS_A_ESCANEAR = 4
MAX_ALERTAS_POR_EJECUCION = 30

# Categorías de Falabella Perú a monitorear
CATEGORIAS = [
    {
        "nombre": "Tecnología",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Tecnologia",
    },
    {
        "nombre": "Celulares",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Celulares-y-Telefonos",
    },
    {
        "nombre": "Electrohogar",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40512/Electrohogar",
    },
    {
        "nombre": "Cómputo y Laptops",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40496/Computo",
    },
    {
        "nombre": "Televisores",
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


def descargar_imagen_bytes(session: requests.Session, imagen_url: str) -> bytes | None:
    """
    Descarga la imagen desde Falabella usando la sesión del script para evitar
    el error 400 ('failed to get HTTP URL content') de los servidores de Telegram.
    """
    if not imagen_url:
        return None
    try:
        headers_img = {
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.falabella.com.pe/",
        }
        resp = session.get(imagen_url, headers=headers_img, timeout=8)
        if resp.ok and resp.content and len(resp.content) > 500:
            return resp.content
    except requests.RequestException:
        pass
    return None


def enviar_alerta_telegram(
    session: requests.Session, mensaje: str, imagen_url: str = ""
) -> bool:
    """
    Envía la alerta subiendo la foto descargada directamente como binario a Telegram,
    o usa texto plano como respaldo si el producto no tiene foto.
    """
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    # 1. Intentar descargar la imagen y subirla como archivo (evita bloqueo de URL en Telegram)
    img_bytes = descargar_imagen_bytes(session, imagen_url)
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
                break
            except requests.RequestException:
                break

    # 2. Respaldo en texto plano si la imagen no existe o falla
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
    """Encuentra la lista de productos dentro del bloque __NEXT_DATA__ de Falabella."""
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


def obtener_productos_pagina(
    session: requests.Session, cat: dict, pagina: int
) -> list:
    """
    Consulta la página de categoría en Falabella Perú aplicando el filtro de
    vendedor oficial ('FALABELLA') y ordenamiento por mayor descuento.
    """
    params = {
        "page": pagina,
        "sortBy": "derived.price.search.discount,desc",
        "f.derived.variant.sellerId": "FALABELLA",  # Filtro nativo solo Falabella Oficial
    }

    try:
        resp = session.get(cat["url"], params=params, timeout=14)
        if resp.ok:
            match = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if match:
                next_data = json.loads(match.group(1))
                return buscar_productos_recursivo(next_data)
    except Exception as e:
        print(f"[WARN] Error consultando {cat['nombre']} (pág {pagina}): {e}")

    return []


def es_vendedor_oficial(prod: dict) -> bool:
    """Verifica que el vendedor sea exclusivamente Falabella Oficial y no un tercero."""
    seller_name = str(prod.get("sellerName") or "").strip().upper()
    seller_id = str(prod.get("sellerId") or "").strip().upper()

    # Si alguno de los campos existe, debe ser explícitamente FALABELLA
    if seller_name and "FALABELLA" not in seller_name:
        return False
    if seller_id and "FALABELLA" not in seller_id:
        return False

    # Descartar productos reacondicionados si vienen infiltrados
    nombre = str(prod.get("displayName") or "").upper()
    if "REACONDICIONADO" in nombre or "REFURBISHED" in nombre:
        return False

    return True


def limpiar_precio(valor_str: str) -> float:
    if not valor_str:
        return 0.0
    limpio = re.sub(r"[^\d.]", "", str(valor_str).replace(",", ""))
    try:
        return float(limpio)
    except ValueError:
        return 0.0


def construir_url_imagen(prod: dict) -> str:
    """Construye la URL válida del CDN de imágenes de Falabella Perú."""
    media_urls = prod.get("mediaUrls", [])
    if media_urls:
        primera = str(media_urls[0]).strip()
        if primera.startswith("http"):
            return primera
        if primera.startswith("//"):
            return f"https:{primera}"
        # En Falabella Perú los IDs de mediaUrls se sirven desde media.falabella.com.pe
        return f"https://media.falabella.com.pe/falabellaPE/{primera}/public"

    prod_id = str(prod.get("productId") or prod.get("skuId") or "").strip()
    if prod_id:
        return f"https://media.falabella.com.pe/falabellaPE/{prod_id}_1/public"
    return ""


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

    url_producto = str(prod.get("url") or "").strip()
    if url_producto and not url_producto.startswith("http"):
        url_producto = f"https://www.falabella.com.pe{url_producto}"
    elif not url_producto:
        prod_id = prod.get("productId", "")
        url_producto = f"https://www.falabella.com.pe/falabella-pe/product/{prod_id}"

    imagen_url = construir_url_imagen(prod)

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

            for cat in CATEGORIAS:
                if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                    break

                for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
                    if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                        break

                    resultados = obtener_productos_pagina(session, cat, pagina)
                    print(
                        f"[INFO] {cat['nombre']} (pág {pagina}): "
                        f"{len(resultados)} productos oficiales escaneados."
                    )

                    if not resultados:
                        break

                    for prod in resultados:
                        if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                            break

                        # Filtro estricto: Solo vendido por Falabella Oficial
                        if not es_vendedor_oficial(prod):
                            continue

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

                            mensaje = (
                                f"🟢 OFERTA FALABELLA OFICIAL (-{descuento:.1f}%)\n\n"
                                f"Producto: {nombre}\n"
                                f"Marca: {marca}\n"
                                f"Vendedor: Falabella\n"
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

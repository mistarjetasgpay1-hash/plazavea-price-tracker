import json
import os
import re
import time
from pathlib import Path
import requests

# 1. Configuración y Credenciales
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN2", "").strip()
CHAT_ID = os.getenv("CHAT_ID2", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError(
        "Faltan las variables TELEGRAM_TOKEN2 o CHAT_ID2 en los Secrets de GitHub."
    )

ARCHIVO_ESTADO = Path("notificados_falabella.json")
UMBRAL_DESCUENTO = 45.0
PAGINAS_A_ESCANEAR = 4
MAX_ALERTAS_POR_EJECUCION = 30

# Búsquedas y categorías activas en Falabella Perú
BUSQUEDAS = [
    {"nombre": "Tecnología", "q": "tecnologia"},
    {"nombre": "Electrohogar", "q": "electrohogar"},
    {"nombre": "Celulares y Smartphones", "q": "smartphones"},
    {"nombre": "Laptops y Cómputo", "q": "laptops"},
    {"nombre": "Televisores", "q": "televisores"},
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


def limpiar_precio(valor_str: str) -> float:
    if not valor_str:
        return 0.0
    limpio = re.sub(r"[^\d.]", "", str(valor_str).replace(",", ""))
    try:
        return float(limpio)
    except ValueError:
        return 0.0


def obtener_productos_pagina(
    session: requests.Session, termino: str, pagina: int
) -> list:
    """
    Obtiene los productos de Falabella Perú probando primero la página web (__NEXT_DATA__)
    y como segundo método la API interna de búsqueda.
    """
    # Método 1: Extracción directa de __NEXT_DATA__ en la URL pública de Falabella Perú
    url_web = "https://www.falabella.com.pe/falabella-pe/search"
    params_web = {
        "Ntt": termino,
        "page": pagina,
        "sortBy": "derived.price.search.discount,desc",
    }

    try:
        resp = session.get(url_web, params=params_web, timeout=12)
        if resp.ok:
            match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if match:
                next_data = json.loads(match.group(1))
                resultados = (
                    next_data.get("props", {})
                    .get("pageProps", {})
                    .get("results", [])
                )
                if resultados:
                    return resultados
    except Exception as e:
        print(f"[DEBUG] Falló lectura de __NEXT_DATA__ en '{termino}' (pág {pagina}): {e}")

    # Método 2 (Respaldo): Endpoint API de búsqueda con prefijo de tienda
    url_api = "https://www.falabella.com.pe/s/browse/v1/search/pe"
    params_api = {
        "Ntt": termino,
        "page": pagina,
        "sortBy": "derived.price.search.discount,desc",
        "zones": "150101",
    }

    try:
        resp_api = session.get(url_api, params=params_api, timeout=10)
        if resp_api.ok:
            return resp_api.json().get("data", {}).get("results", [])
        print(f"[WARN] API devolvió HTTP {resp_api.status_code} para '{termino}' (pág {pagina})")
    except Exception as e:
        print(f"[WARN] Error en API para '{termino}' (pág {pagina}): {e}")

    return []


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
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
                    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
                    "Referer": "https://www.falabella.com.pe/falabella-pe",
                }
            )

            for item in BUSQUEDAS:
                if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                    break

                for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
                    if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                        break

                    resultados = obtener_productos_pagina(session, item["q"], pagina)
                    print(
                        f"[INFO] {item['nombre']} (pág {pagina}): "
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

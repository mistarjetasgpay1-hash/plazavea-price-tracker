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

# URLs directas de categorías en Falabella Perú
CATEGORIAS = [
    {
        "nombre": "Tecnología",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Tecnologia",
        "cat_id": "cat760706",
    },
    {
        "nombre": "Electrohogar",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40512/Electrohogar",
        "cat_id": "cat40512",
    },
    {
        "nombre": "Celulares",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Celulares-y-Telefonos",
        "cat_id": "cat760706",
    },
    {
        "nombre": "Cómputo y Laptops",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40496/Computo",
        "cat_id": "cat40496",
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
    """
    Busca automáticamente la lista de productos dentro de cualquier estructura JSON
    de Falabella identificando objetos que contengan 'productId' y 'prices'.
    """
    if isinstance(nodo, list):
        if nodo and isinstance(nodo[0], dict) and "productId" in nodo[0] and "prices" in nodo[0]:
            return nodo
        for elemento in nodo:
            hallado = buscar_productos_recursivo(elemento)
            if hallado:
                return hallado
    elif isinstance(nodo, dict):
        # Priorizar llaves conocidas de Falabella
        for llave_prioritaria in ("results", "products", "searchData", "categoryData", "data"):
            if llave_prioritaria in nodo:
                hallado = buscar_productos_recursivo(nodo[llave_prioritaria])
                if hallado:
                    return hallado
        for valor in nodo.values():
            hallado = buscar_productos_recursivo(valor)
            if hallado:
                return hallado
    return []


def obtener_productos_falabella(
    session: requests.Session, cat: dict, pagina: int
) -> list:
    """Extrae los productos de Falabella Perú desde HTML (__NEXT_DATA__) o API con prefijo."""
    params_web = {
        "page": pagina,
        "sortBy": "derived.price.search.discount,desc",
    }

    try:
        resp = session.get(cat["url"], params=params_web, timeout=14)
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
                if pagina == 1:
                    page_props = next_data.get("props", {}).get("pageProps", {})
                    print(
                        f"[DEBUG] __NEXT_DATA__ leído en {cat['nombre']}, "
                        f"llaves en pageProps: {list(page_props.keys())}"
                    )
            else:
                if pagina == 1:
                    titulo = re.findall(r"<title>(.*?)</title>", resp.text, re.IGNORECASE)
                    print(
                        f"[DEBUG] HTML sin __NEXT_DATA__ en {cat['nombre']} "
                        f"(HTTP {resp.status_code}, Título: {titulo})"
                    )
        else:
            print(f"[WARN] Web devolvió HTTP {resp.status_code} en {cat['nombre']} (pág {pagina})")
    except Exception as e:
        print(f"[WARN] Error leyendo web {cat['nombre']} (pág {pagina}): {e}")

    # Respaldo: API con el prefijo /falabella-pe/ requerido en Perú
    url_api = "https://www.falabella.com.pe/falabella-pe/s/browse/v1/listing/pe"
    params_api = {
        "categoryId": cat["cat_id"],
        "page": pagina,
        "sortBy": "derived.price.search.discount,desc",
        "zones": "150101",
    }
    try:
        resp_api = session.get(url_api, params=params_api, timeout=10)
        if resp_api.ok:
            return buscar_productos_recursivo(resp_api.json())
    except Exception:
        pass

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
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
                    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
                    "Referer": "https://www.falabella.com.pe/falabella-pe",
                }
            )

            for cat in CATEGORIAS:
                if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                    break

                for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
                    if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                        break

                    resultados = obtener_productos_falabella(session, cat, pagina)
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

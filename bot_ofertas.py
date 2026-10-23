import json
import os
import time
from pathlib import Path
import requests

# 1. Configuración y Credenciales (seguro para repositorios públicos)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError(
        "Faltan las variables TELEGRAM_TOKEN o CHAT_ID en los Secrets de GitHub."
    )

API_URL = "https://www.plazavea.com.pe/api/io/_v/api/intelligent-search/product_search/"
ARCHIVO_ESTADO = Path("notificados.json")
UMBRAL_DESCUENTO = 49.0
PAGINAS_A_ESCANEAR = 8  # 8 páginas x 50 productos = 400 productos por ejecución


def cargar_notificados() -> set:
    """Carga los IDs o enlaces de ofertas ya notificadas desde el archivo JSON."""
    if not ARCHIVO_ESTADO.exists():
        return set()
    try:
        with ARCHIVO_ESTADO.open("r", encoding="utf-8") as f:
            datos = json.load(f)
            return set(datos) if isinstance(datos, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def guardar_notificados(notificados: set) -> None:
    """Guarda la lista ordenada de ofertas notificadas para mantener un git diff limpio."""
    with ARCHIVO_ESTADO.open("w", encoding="utf-8") as f:
        json.dump(sorted(notificados), f, indent=2, ensure_ascii=False)


def post_telegram_con_reintento(
    session: requests.Session, endpoint: str, payload: dict
) -> bool:
    """Envía la petición a Telegram manejando límites de velocidad (HTTP 429)."""
    for _ in range(2):
        try:
            resp = session.post(endpoint, json=payload, timeout=8)
            if resp.ok:
                return True
            if resp.status_code == 429:
                espera = resp.json().get("parameters", {}).get("retry_after", 5)
                print(f"[RATE LIMIT] Esperando {espera}s solicitados por Telegram...")
                time.sleep(espera + 1)
                continue
            return False
        except requests.RequestException as e:
            print(f"[WARN] Error de red con Telegram: {e}")
            return False
    return False


def enviar_alerta_telegram(
    session: requests.Session, mensaje: str, imagen_url: str = ""
) -> bool:
    """
    Envía la alerta con foto (sendPhoto) si hay imagen disponible.
    Si el producto no tiene foto o falla la carga, usa sendMessage como respaldo.
    """
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    # 1. Intentar envío con imagen (sendPhoto)
    if imagen_url:
        payload_foto = {
            "chat_id": CHAT_ID,
            "photo": imagen_url,
            "caption": mensaje[:1024],
        }
        if post_telegram_con_reintento(session, f"{base_url}/sendPhoto", payload_foto):
            return True
        print("[WARN] Falló envío con foto. Reintentando en solo texto...")

    # 2. Respaldo: envío en texto plano (sendMessage)
    payload_texto = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "disable_web_page_preview": False,
    }
    return post_telegram_con_reintento(session, f"{base_url}/sendMessage", payload_texto)


def construir_url_producto(producto: dict) -> str:
    """Construye el enlace absoluto hacia el producto en Plaza Vea."""
    link = producto.get("link", "")
    if link.startswith("http"):
        return link
    slug = producto.get("linkText", "").strip("/")
    return (
        f"https://www.plazavea.com.pe/{slug}/p"
        if slug
        else "https://www.plazavea.com.pe"
    )


def extraer_mejor_oferta(producto: dict) -> tuple[float, float, int, str]:
    """
    Recorre los SKUs y vendedores del producto para obtener:
    (list_price, selling_price, stock_disponible, imagen_url)
    """
    items = producto.get("items", [])
    for item in items:
        imagenes = item.get("images", [])
        imagen_url = imagenes[0].get("imageUrl", "").strip() if imagenes else ""

        sellers = item.get("sellers", [])
        for seller in sellers:
            oferta = seller.get("commertialOffer", {})
            list_price = float(oferta.get("ListPrice") or 0.0)
            selling_price = float(oferta.get("Price") or 0.0)
            stock = int(oferta.get("AvailableQuantity") or 0)

            if stock > 0 and list_price > 0 and selling_price > 0:
                return list_price, selling_price, stock, imagen_url

    return 0.0, 0.0, 0, ""


def monitorear_ofertas() -> None:
    inicio = time.monotonic()
    notificados = cargar_notificados()
    nuevos_registros = False
    alertas_enviadas = 0

    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            }
        )

        for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
            params = {
                "page": pagina,
                "count": 50,
                "sort": "discount:desc",
            }

            try:
                respuesta = session.get(API_URL, params=params, timeout=6)
                respuesta.raise_for_status()
                data = respuesta.json()
            except (requests.RequestException, ValueError) as e:
                print(f"[WARN] Error consultando página {pagina}: {e}")
                continue

            productos = data.get("products", [])
            if not productos:
                break

            for prod in productos:
                url_producto = construir_url_producto(prod)
                prod_id = str(prod.get("productId", url_producto))
                clave_unica = f"{prod_id}|{url_producto}"

                if clave_unica in notificados:
                    continue

                precio_lista, precio_oferta, stock, imagen_url = extraer_mejor_oferta(
                    prod
                )
                if (
                    precio_lista <= 0
                    or precio_oferta <= 0
                    or precio_oferta >= precio_lista
                ):
                    continue

                descuento = ((precio_lista - precio_oferta) / precio_lista) * 100.0

                if descuento > UMBRAL_DESCUENTO:
                    nombre = prod.get("productName", "Producto sin nombre").strip()
                    marca = prod.get("brand", "Genérico").strip()

                    mensaje = (
                        f"🔥 OFERTA DETECTADA (-{descuento:.1f}%)\n\n"
                        f"Producto: {nombre}\n"
                        f"Marca: {marca}\n"
                        f"Precio Lista: S/ {precio_lista:,.2f}\n"
                        f"Precio Oferta: S/ {precio_oferta:,.2f}\n"
                        f"Stock reportado: {stock} unid.\n\n"
                        f"Enlace: {url_producto}"
                    )

                    if enviar_alerta_telegram(session, mensaje, imagen_url):
                        notificados.add(clave_unica)
                        nuevos_registros = True
                        alertas_enviadas += 1
                        print(f"[OK] ({alertas_enviadas}) {nombre} (-{descuento:.1f}%)")
                        # Pausa breve para respetar el límite de mensajes por segundo de Telegram
                        time.sleep(0.35)

    if nuevos_registros:
        guardar_notificados(notificados)
        print(f"[INFO] Se guardaron {alertas_enviadas} ofertas nuevas en notificados.json.")
    else:
        print("[INFO] Sin ofertas nuevas que superen el umbral.")

    duracion = time.monotonic() - inicio
    print(f"[FIN] Escaneo de {PAGINAS_A_ESCANEAR} páginas completado en {duracion:.2f} s.")


if __name__ == "__main__":
    monitorear_ofertas()

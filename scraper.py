import json
import os
import time
from pathlib import Path
import requests

# 1. Configuración y Credenciales (con fallback local)
TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN", "8837743219:AAEUvwq2EoWnFPDO-2u4-Af6on9S0gxfvFA"
)
CHAT_ID = os.getenv("CHAT_ID", "6820786359")

API_URL = "https://www.plazavea.com.pe/api/io/_v/api/intelligent-search/product_search/"
ARCHIVO_ESTADO = Path("notificados.json")
UMBRAL_DESCUENTO = 49.0
PAGINAS_A_ESCANEAR = 3  # 50 productos por página = 150 productos evaluados en ~2 segundos


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


def enviar_alerta_telegram(session: requests.Session, mensaje: str) -> bool:
    """Envía un mensaje en texto plano (sin parse_mode) a Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "disable_web_page_preview": False,
    }
    try:
        resp = session.post(url, json=payload, timeout=5)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[ERROR] Fallo al enviar mensaje a Telegram: {e}")
        return False


def construir_url_producto(producto: dict) -> str:
    """Construye el enlace absoluto hacia el producto en Plaza Vea."""
    link = producto.get("link", "")
    if link.startswith("http"):
        return link
    slug = producto.get("linkText", "").strip("/")
    return f"https://www.plazavea.com.pe/{slug}/p" if slug else "https://www.plazavea.com.pe"


def extraer_mejor_oferta(producto: dict) -> tuple[float, float, int]:
    """
    Recorre los SKUs y vendedores del producto para obtener:
    (list_price, selling_price, stock_disponible)
    """
    items = producto.get("items", [])
    for item in items:
        sellers = item.get("sellers", [])
        for seller in sellers:
            oferta = seller.get("commertialOffer", {})
            list_price = float(oferta.get("ListPrice") or 0.0)
            selling_price = float(oferta.get("Price") or 0.0)
            stock = int(oferta.get("AvailableQuantity") or 0)

            if stock > 0 and list_price > 0 and selling_price > 0:
                return list_price, selling_price, stock

    return 0.0, 0.0, 0


def monitorear_ofertas() -> None:
    inicio = time.monotonic()
    notificados = cargar_notificados()
    nuevos_registros = False

    # Reutilizar la conexión TCP/TLS acelera las consultas drásticamente
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
                respuesta = session.get(API_URL, params=params, timeout=5)
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

                precio_lista, precio_oferta, stock = extraer_mejor_oferta(prod)
                if precio_lista <= 0 or precio_oferta <= 0 or precio_oferta >= precio_lista:
                    continue

                descuento = ((precio_lista - precio_oferta) / precio_lista) * 100.0

                if descuento > UMBRAL_DESCUENTO:
                    nombre = prod.get("productName", "Producto sin nombre").strip()
                    marca = prod.get("brand", "Genérico").strip()

                    # Texto plano sin parse_mode para evitar errores con símbolos especiales
                    mensaje = (
                        f"🔥 OFERTA DETECTADA (-{descuento:.1f}%)\n\n"
                        f"Producto: {nombre}\n"
                        f"Marca: {marca}\n"
                        f"Precio Lista: S/ {precio_lista:,.2f}\n"
                        f"Precio Oferta: S/ {precio_oferta:,.2f}\n"
                        f"Stock reportado: {stock} unid.\n\n"
                        f"Enlace: {url_producto}"
                    )

                    if enviar_alerta_telegram(session, mensaje):
                        notificados.add(clave_unica)
                        nuevos_registros = True
                        print(f"[OK] Notificado: {nombre} (-{descuento:.1f}%)")

    # Solo escribe en disco si hubo cambios reales para optimizar el paso de Git
    if nuevos_registros:
        guardar_notificados(notificados)
        print("[INFO] Archivo notificados.json actualizado.")
    else:
        print("[INFO] Sin ofertas nuevas que superen el umbral.")

    duracion = time.monotonic() - inicio
    print(f"[FIN] Ejecución completada en {duracion:.2f} segundos.")


if __name__ == "__main__":
    monitorear_ofertas()

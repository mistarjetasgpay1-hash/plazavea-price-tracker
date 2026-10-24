# plazavea-price-tracker

Monitor de ofertas de **Plaza Vea** con alertas por Telegram. Corre en GitHub Actions, sin servidor.

## Qué hace

1. Recorre las categorías configuradas en `config.yaml`.
2. **Filtro estricto de tienda oficial:** en el servidor, la faceta VTEX `vendido-por/plazavea` y `vendido-por/oechsle`; en el código, solo se acepta la oferta de un vendedor con `sellerId` `1` (Plaza Vea) u `oechsle` que tenga stock. Promart y el resto del marketplace quedan fuera aunque VTEX los devuelva.
3. Descarta lo que no llega al umbral de descuento, no tiene stock o contiene palabras excluidas.
4. Consulta el historial (TTL) y envía a Telegram solo lo nuevo o lo que **bajó aún más de precio**.
5. Guarda el historial en la rama `state` (fuera de `main`) y publica un resumen en la pestaña *Actions*.

**Categorías:** consumo diario (abarrotes, desayunos, lácteos, quesos y fiambres, carnes, frutas y verduras, congelados, panadería, bebidas, licores, limpieza, cuidado personal, bebé, mascotas) y además tecnología, electrohogar, juguetes, moda (mujer, hombre, infantil), accesorios, zapatillas, zapatos, deportes, belleza, hogar (deco, dormitorio, muebles, terraza, mejoramiento), librería y automotriz. Siempre vendidos por Plaza Vea u Oechsle.

La API devuelve los productos ordenados por descuento. En cada corrida se leen las `fixed_pages` primeras páginas de cada categoría (los mayores descuentos) más `rotating_pages` más profundas, que avanzan corrida a corrida hasta `max_pages` o hasta que el descuento baja del umbral. Así se cubren todas las ofertas sin leer 40 páginas cada 15 minutos.

## Estructura

```
├── .github/workflows/
│   ├── monitor.yml      # cron + ejecución + historial en la rama `state`
│   └── ci.yml           # pytest en cada push
├── config.yaml          # umbrales, TTL, fuentes, grupos ← lo único que normalmente editas
├── tracker/
│   ├── __main__.py      # CLI: python -m tracker [--dry-run] [--max-alerts N] [-v]
│   ├── config.py        # carga/valida YAML + secretos
│   ├── models.py        # Offer (modelo normalizado)
│   ├── http_client.py   # reintentos, backoff con jitter, pausas corteses
│   ├── scraper.py       # extracción específica de la tienda
│   ├── base.py          # interfaz común de scrapers
│   ├── pricing.py       # parseo de precios y % de descuento
│   ├── filters.py       # tienda oficial, stock, umbral, palabras excluidas
│   ├── state.py         # historial con TTL + regla de bajada de precio
│   ├── telegram.py      # envío con throttle y manejo de 429
│   ├── formatter.py     # plantillas HTML de los mensajes
│   └── pipeline.py      # orquestador + resumen de la corrida
└── tests/               # pytest (sin red)
```

## Historial con TTL

Cada producto notificado se guarda con su precio y la fecha del aviso (`state/<tienda>.json`, rama `state`).

| Situación | ¿Se notifica? |
|---|---|
| Producto nuevo | ✅ |
| Su precio bajó ≥ `renotify_drop_pct` (3 %) desde el último aviso | ✅ `📉 ¡Bajó más!` |
| Mismo precio o subió | ❌ |
| Pasaron `ttl_days` (3) desde el último aviso y sigue en oferta | ✅ (el registro vence y se purga solo) |

El TTL es por producto, así que no hay una avalancha de re-avisos el mismo día. Para empezar de cero, borra la rama `state`.

## Telegram

- Secrets (Settings → Secrets and variables → Actions): `TELEGRAM_TOKEN` y `CHAT_ID`.
- Cada mensaje lleva hashtags por grupo y categoría (ej. `#PlazaVea_Alimentos #Abarrotes`, `#PlazaVea_Moda #ModaMujer`). Puedes buscar o filtrar por ellos dentro del chat.
- Por corrida: `max_individual_alerts` alertas con foto (repartidas entre categorías) y el resto, hasta `digest_max_items`, en **resúmenes por categoría** de hasta 30 ofertas cada uno. Lo que no entra se envía en la siguiente corrida.
- **Temas (opcional):** si conviertes el chat en un grupo con Temas, pon el `thread_id` de cada tema en `telegram.groups` y las alertas se separan solas.
- Si 3 corridas seguidas no obtienen datos (bloqueo o cambio de la web), llega un aviso ⚠️ silencioso.

## Uso

```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # tests
python -m tracker --dry-run -v            # extrae y filtra sin enviar ni guardar
```

En GitHub: *Actions → Monitor → Run workflow* (marca `dry_run` para una simulación).

## Personalizar

- **Umbral:** `offers.min_discount_pct` (global) o `min_discount` por categoría.
- **Categorías:** agrega o comenta líneas en `scraper` dentro de `config.yaml`.
- **Frecuencia:** `cron` en `.github/workflows/monitor.yml` (hora UTC; Lima = UTC-5). El cron de GitHub es de "mejor esfuerzo" y puede retrasarse algunos minutos.

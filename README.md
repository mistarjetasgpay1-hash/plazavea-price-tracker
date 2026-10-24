# plazavea-price-tracker

Monitor de ofertas de **Plaza Vea** con alertas por Telegram: dos bots que corren 24/7 en un VPS, cada 5 minutos, **solo con productos vendidos por la tienda oficial** (Plaza Vea u Oechsle; nunca terceros ni marketplace).

| Bot | Script | Telegram | Qué vigila | Umbral |
|---|---|---|---|---|
| **Canasta básica** | `run_alimentos.py` | `TELEGRAM_TOKEN3` / `CHAT_ID3` | abarrotes (arroz, azúcar, aceites, atún, fideos…), lácteos y huevos, desayuno | **15 %** |
| | | | aseo y limpieza (detergente, lejía, suavizante, shampoo, toallas higiénicas…), pañales | **20 %** |
| **General** | `run_general.py` | `TELEGRAM_TOKEN` / `CHAT_ID` | tecnología, electro, juguetes, moda, hogar, frescos, bebidas, bebé, mascotas… | 50 % |

## Qué incluye cada alerta

```
🛒 -32% con Tarjeta Oh! · Fideos DON VITTORIO Spaghetti Bolsa 950g
🏷️ DON VITTORIO · Vendido por Plaza Vea
💰 Regular S/ 6.00 → Online S/ 5.40 (−10%)
💳 Tarjeta Oh!: S/ 4.08 (−32%)
🎁 Compra 1 y lleva Galletas Saladitas BELL'S Paquete 6un a S/ 2.49 (−22%)
⚖️ S/ 4.29 x kg
💳 Exclusivo con Tarjeta Oh!        (o ✅ Cualquier medio de pago)
🔗 Ver en Plaza Vea
#PlazaVea_Abarrotes
```

- **Medio de pago:** "Cualquier medio de pago" si el precio online ya llega al umbral. "Exclusivo con Tarjeta Oh!" si solo llega con el precio de tarjeta.
- **Precio con Tarjeta Oh!:** sale del teaser VTEX `TARJETA OH - PVEA` / `Promo Oh-Pay` (precio regular − descuento del teaser). Coincide con el que muestra la web.
- **Combos y promociones complejas** (`promotions/processor.py`):
  - `#PAGAXLLEVAY#`: compra X y lleva Y con N % (el nombre y el precio de Y se consultan en el catálogo).
  - Mismo producto: 2x1 o "2da unidad al N %".
  - Descuento por cantidad mínima.
  - Textos como "3x2", "2 x S/ 10", "2do al 70%" o "Lleva … a S/ 9.90" en teasers o sellos del producto.
  - Una oferta también califica si el **ahorro real del combo** (sobre todo lo que compras) llega al umbral.
- **Exclusiones** (`offers.exclude_keywords` y `offers.exclude_tags`): en canasta básica se descartan los dulces de Halloween, tanto por el nombre como por las colecciones de campaña de Plaza Vea ("plazaVea | Halloween | …", "Especial Halloween"). Se pueden agregar otras campañas o categorías con el mismo mecanismo.
- **Precio por kilo, litro o unidad:** se calcula desde el nombre ("Bolsa 950g", "Caja 9un x 24g", "Doypack 3L", "Paquete 24un") o desde la propiedad `Contenido Neto`. Los productos a granel ya vienen por kg.

## Historial (TTL + bajada de precio)

| Situación | ¿Se notifica? |
|---|---|
| Producto nuevo | ✅ |
| Ya avisado, pero su precio bajó ≥ 3 % desde el último aviso | ✅ de inmediato `📉 ¡Bajó más!` |
| Ya avisado, mismo precio o más caro | ❌ durante 3 días |
| Pasaron 3 días y sigue en oferta | ✅ |

Si un bot no tiene historial (`state/plazavea_<bot>.json`), lo **migra** automáticamente desde `state/plazavea.json` (formato v2) y `notificados.json` (formato antiguo), sin perder lo ya avisado.

## Paginado adaptativo

La API devuelve los productos ordenados por mayor descuento online.

- **Cabecera adaptativa** (`fixed_pages`): el bot sigue leyendo mientras haya ofertas que califiquen y se detiene tras `tail_pages` páginas seguidas sin ninguna.
- **Barrido profundo** (`rotating_pages` hasta `max_pages`): avanza unas páginas por corrida. En canasta básica (`sweep_deep: true`) recorre todo el fondo para encontrar combos y precios con tarjeta, que aparecen dispersos.

## Producción en el VPS (24/7, cada 5 minutos)

```bash
git clone https://github.com/mistarjetasgpay1-hash/plazavea-price-tracker.git
cd plazavea-price-tracker
bash deploy/setup_vps.sh      # 1ª vez: crea .env y se detiene
nano .env                     # TELEGRAM_TOKEN / CHAT_ID (general) y TELEGRAM_TOKEN3 / CHAT_ID3 (canasta)
bash deploy/setup_vps.sh      # instala, migra el historial, prueba y arranca PM2
pm2 startup                   # (una vez) para que arranquen solos al reiniciar el VPS
```

| Acción | Comando |
|---|---|
| Estado | `pm2 status` |
| Logs en vivo | `pm2 logs plazavea-alimentos` (o `plazavea-general`) |
| Actualizar tras cambios en GitHub | `bash deploy/setup_vps.sh` |
| Tras editar `config*.yaml` | nada: se recarga en cada ciclo |
| Tras editar `.env` | `pm2 restart all` |

**Modo continuo (`--loop 300`):**

- Cada corrida empieza 5 minutos después de la anterior. Si una se pasa, la siguiente empieza ya.
- Un error de red, de Telegram o un cambio en la web no detiene el bucle. Tras 3 corridas sin datos llega un aviso ⚠️.
- `pm2 stop` / `restart` detienen limpio y guardan el historial.
- Un candado (`state/*.lock`) impide dos instancias del mismo bot. `--dry-run` sí puede correr junto a PM2.

## GitHub Actions

Sin horario (la producción está en el VPS):

- **Push a `main`** → prueba de integración real de ambos bots en simulación: no envía nada. El resumen muestra qué ofertas califican y por qué (cualquier medio, solo tarjeta o combo).
- **Manual** → eliges el bot. Por defecto es una simulación.
- `ci.yml` corre `pytest` en Python 3.11 y 3.12.

## Estructura

```
├── run_alimentos.py / run_general.py      # ejecutables (aceptan --loop, --dry-run, -v)
├── config.yaml                             # común (heredado por ambos bots)
├── config.alimentos.yaml / config.general.yaml
├── ecosystem.config.js · deploy/setup_vps.sh · .env.example
├── src/tracker/
│   ├── core/          config (YAML con extends) · models · pricing · filters · http_client · pipeline · runner
│   ├── scrapers/      base.py (interfaz) · vtex.py (Plaza Vea: filtro oficial, paginado, catálogo)
│   ├── promotions/    processor.py (Tarjeta Oh!, combos, NxM, 2da unidad) · units.py (precio por kg/L/unidad)
│   ├── telegram/      client.py (envío, 429, throttle) · formatter.py (plantillas)
│   └── state/         store.py (historial TTL + bajada, migración, candado)
└── tests/             pytest sin red, con respuestas reales recortadas de la API
```

## Desarrollo local

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python run_alimentos.py --dry-run -v
```

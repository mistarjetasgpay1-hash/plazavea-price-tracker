# plazavea-price-tracker

Monitor de ofertas de **Plaza Vea** con alertas por Telegram: dos bots que corren en **GitHub Actions** (cada 15 min de 06:00 a 00:59 y cada hora de madrugada, hora de Lima), **solo con productos vendidos por la tienda oficial** (Plaza Vea u Oechsle; nunca terceros ni marketplace).

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
- **Exclusiones** (`offers.exclude_keywords` y `offers.exclude_tags`): en canasta básica se descartan:
  - los dulces de Halloween, por el nombre, por la categoría ("/O/dulces de halloween/") o por las colecciones de campaña de Plaza Vea ("plazaVea | Halloween | …", "Especial Halloween");
  - las golosinas: caramelos y chupetes, gomitas, marshmallows y otras.

  Galletas, chocolates y snacks siguen entrando. Se pueden quitar otras campañas o categorías agregando su ruta o nombre (regex) en `config.alimentos.yaml`.
- **Precio por kilo, litro o unidad:** se calcula desde el nombre ("Bolsa 950g", "Caja 9un x 24g", "Doypack 3L", "Paquete 24un", "Lata 80g Paquete 3un" = 240 g) o desde la propiedad `Contenido Neto`. Los productos a granel ya vienen por kg. Si el pack es ambiguo ("Caldo 100g Paquete 25un": ¿100 g en total o por unidad?) o mezcla productos ("Papas 185g + Snax 230g"), no se muestra precio unitario antes que mostrar uno falso.

## Historial (TTL + bajada de precio)

| Situación | ¿Se notifica? |
|---|---|
| Producto nuevo | ✅ |
| Ya avisado, pero su precio bajó ≥ 3 % desde el último aviso | ✅ de inmediato `📉 ¡Bajó más!` |
| Ya avisado y aparece un precio con Tarjeta Oh! ≥ 3 % menor que el mejor precio avisado | ✅ `📉 ¡Bajó más! Con Tarjeta Oh! ahora…` |
| Ya avisado, mismo precio o más caro | ❌ durante 3 días |
| Pasaron 3 días y sigue en oferta | ✅ |

Si un bot no tiene historial (`state/plazavea_<bot>.json`), lo **migra** automáticamente desde `state/plazavea.json` (formato v2) y `notificados.json` (formato antiguo), sin perder lo ya avisado.

## Alertas en dos fases (más rápido, sin peticiones extra)

1. **Fase 1:** lee la página 1 de todas las categorías (la de mayores descuentos) y envía de inmediato las alertas con foto. Las mejores ofertas salen a los pocos segundos de empezar la corrida, no al final.
2. **Fase 2:** sigue con el resto del paginado; usa las fotos que queden del cupo (`max_individual_alerts`) y manda los resúmenes por grupo.

El log y el resumen de Actions muestran a qué segundo salió la primera alerta.

## Paginado adaptativo

La API devuelve los productos ordenados por mayor descuento online (máximo 2 500 por búsqueda: desde la página 51 responde HTTP 400).

- **Cabecera adaptativa** (`fixed_pages`): el bot sigue leyendo mientras haya ofertas que califiquen y se detiene tras `tail_pages` páginas seguidas sin ninguna.
- **Barrido profundo** (`rotating_pages` hasta `max_pages`): avanza unas páginas por corrida. En canasta básica (`sweep_deep: true`) recorre todo el fondo para encontrar combos y precios con tarjeta, que aparecen dispersos.

## Producción en un VPS (opcional, 24/7 cada 5 minutos)

> Hoy la producción es **GitHub Actions** (sección siguiente). Esto queda solo por si vuelves a un VPS:
> en ese caso quita el `schedule:` de `monitor.yml` para no tener dos instancias avisando a la vez.

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
| Actualizar tras cambios en GitHub | `bash deploy/setup_vps.sh` (si `git pull` falla por cambios locales, se detiene y no reinicia nada) |
| Ajustes propios del VPS | créalos en `config.alimentos.local.yaml` / `config.general.local.yaml` (empiezan con `extends: config.alimentos.yaml`; no se suben a GitHub ni bloquean las actualizaciones) |
| Tras editar `config*.yaml` | nada: se recarga en cada ciclo. Si queda con errores (clave mal escrita, falta `path`…), el bot lo registra y sigue con la última configuración válida |
| Tras editar `.env` | `pm2 restart all` |

**Modo continuo (`--loop 300`):**

- Cada corrida empieza 5 minutos después de la anterior. Si una se pasa, la siguiente empieza ya.
- Un error de red, de Telegram o un cambio en la web no detiene el bucle. Un producto con datos raros se ignora y un error en una categoría no afecta a las demás.
- **Avisos de salud ⚠️** (una vez, y ✅ al recuperarse): tras 3 corridas sin datos (incluye que la API responda pero vacía, como pasa si Plaza Vea renombra una ruta o el filtro de vendedor) y tras 3 corridas en que una categoría no devuelve productos.
- Si Telegram pide esperar más de 30 s (error 429), el bot deja los envíos restantes para la próxima corrida en vez de quedarse congelado.
- `pm2 stop` / `restart` detienen limpio y guardan el historial.
- Un candado (`state/*.lock`) impide dos instancias del mismo bot. `--dry-run` sí puede correr junto a PM2.

**Monitor externo (opcional, recomendado):** crea en [healthchecks.io](https://healthchecks.io) (gratis) un check por bot con periodo 5 min y gracia 10 min, y pega su URL de ping en `.env` (`HEALTHCHECK_URL_ALIMENTOS`, `HEALTHCHECK_URL_GENERAL`) y luego `pm2 restart all`. Cada corrida avisa "estoy vivo" (o `/fail` si no obtuvo datos o no pudo enviar nada a Telegram); si el VPS se cae o PM2 se detiene, healthchecks.io te avisa por correo o Telegram.

## GitHub Actions

`monitor.yml` es la **producción**:

- **Horario (cron)** → corrida **real** de ambos bots: envía a Telegram y guarda el historial en las ramas `state-alimentos` / `state-general`. Las corridas reales se encolan, nunca se cancelan entre sí.
- **Manual** → eliges el bot; por defecto es **real**. Marca *Simulación* si solo quieres probar.
- **Push a `main`** → prueba de integración en **simulación**: no envía nada (el log y el resumen lo dicen con 🧪 SIMULACIÓN).
- `ci.yml` corre `pytest` en Python 3.11 y 3.12.

**Diagnóstico en el log de cada corrida** (paso *Ejecutar bot*):

| Línea | Qué te dice |
|---|---|
| `Secreto TELEGRAM_TOKEN3: definido (46 caracteres)` | el Secret llega al job (o `::error` si está vacío) |
| `[TG] Token: variable TELEGRAM_TOKEN3 (definida…) · chat_id: variable CHAT_ID3 (definida)` | de qué variable leyó el bot cada secreto |
| `[TG] ← getMe · HTTP 200 · bot @…` / `[TG] ← getChat · HTTP 200 · chat "…"` | Telegram acepta el token y el chat (si no, la corrida termina en **rojo**) |
| `[FILTROS] Total: N de M productos superaron los filtros…` | cuántas ofertas califican y por qué se descartó el resto |
| `[TG] Invocando envío a Telegram: X alertas con foto + Y en resúmenes` | se llamó a la función de envío (o `Nada que enviar…`) |
| `[TG] ← sendPhoto · HTTP 200 · ok=true · message_id=…` | respuesta HTTP real de Telegram por cada mensaje |
| `[TG] Respuestas de la API de Telegram en esta corrida: … HTTP 200: 26` | totales por código HTTP (también en el resumen del run) |

GitHub desactiva los workflows con `schedule` de un repositorio público tras 60 días sin actividad en el repo: si eso pasa, actívalo de nuevo desde la pestaña Actions.

## Estructura

```
├── run_alimentos.py / run_general.py      # ejecutables (aceptan --loop, --dry-run, -v)
├── config.yaml                             # común (heredado por ambos bots)
├── config.alimentos.yaml / config.general.yaml
├── ecosystem.config.js · deploy/setup_vps.sh · .env.example
├── src/tracker/
│   ├── core/          config (YAML con extends) · models · pricing · filters · http_client · pipeline · runner · healthcheck
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

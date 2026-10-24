# plazavea-price-tracker

Monitor de ofertas de **Plaza Vea** con alertas por Telegram. Son dos bots que corren 24/7 en un VPS, cada 5 minutos:

| Bot | Script | Configuración | Qué vigila |
|---|---|---|---|
| **Alimentos** | `run_alimentos.py` | `config.alimentos.yaml` | abarrotes, desayunos, lácteos, quesos y fiambres, carnes, frutas y verduras, congelados, panadería, bebidas, licores, limpieza, cuidado personal, bebé, mascotas |
| **General** | `run_general.py` | `config.general.yaml` | tecnología, electrohogar, juguetes, moda (mujer, hombre, infantil), accesorios, zapatillas, zapatos, deportes, belleza, hogar, librería, automotriz |

Lo común (umbral, TTL, Telegram, vendedores) está en `config.yaml`, del que heredan ambos.

## Qué hace cada corrida

1. Recorre sus categorías en la API de Plaza Vea, ordenadas por mayor descuento.
2. **Filtro estricto de tienda oficial:** en el servidor, la faceta `vendido-por/plazavea` y `vendido-por/oechsle`; en el código, solo se acepta la oferta de un vendedor con `sellerId` `1` (Plaza Vea) u `oechsle` que tenga stock. Promart y el resto del marketplace quedan fuera.
3. Descarta lo que no llega al umbral (50 %), no tiene stock o contiene palabras excluidas.
4. Consulta el historial (TTL) y envía solo lo nuevo o lo que **bajó aún más de precio**.
5. Guarda el historial en `state/plazavea_<bot>.json`.

**Paginado:** en cada corrida se leen las `fixed_pages` primeras páginas de cada categoría (los mayores descuentos) más `rotating_pages` más profundas, que avanzan corrida a corrida hasta `max_pages` o hasta que el descuento baja del umbral.

## Producción en el VPS (24/7, cada 5 minutos)

Requisitos: Linux con Python 3.11+, `python3-venv`, Node.js (para PM2) y git.

```bash
git clone https://github.com/mistarjetasgpay1-hash/plazavea-price-tracker.git
cd plazavea-price-tracker
bash deploy/setup_vps.sh      # 1ª vez: crea .env y se detiene
nano .env                     # pon TELEGRAM_TOKEN y CHAT_ID
bash deploy/setup_vps.sh      # instala dependencias, migra el historial, prueba y arranca PM2
pm2 startup                   # (una vez) para que arranquen solos al reiniciar el VPS
```

El script es re-ejecutable: para **actualizar** tras cambios en GitHub, vuelve a correr `bash deploy/setup_vps.sh`.

| Acción | Comando |
|---|---|
| Estado | `pm2 status` |
| Logs en vivo | `pm2 logs plazavea-general` (o `plazavea-alimentos`) |
| Reiniciar tras editar `config*.yaml` | no hace falta: la config se recarga en cada ciclo |
| Reiniciar tras editar `.env` | `pm2 restart all` |
| Detener | `pm2 stop all` |

**Cómo funciona el modo continuo (`--loop 300`):**

- La cadencia es fija de inicio a inicio: cada corrida empieza 5 minutos después de la anterior. Si una dura 70 s, la pausa es de 230 s. Si una se pasa de 5 minutos, la siguiente empieza de inmediato.
- Un error (red, Telegram, cambio en la web) se registra y el bucle sigue. Si 3 corridas seguidas no obtienen datos, llega un aviso ⚠️ silencioso al chat.
- Con `pm2 stop` / `restart` el bot se detiene limpio y guarda el historial de lo ya enviado.
- Un candado (`state/*.lock`) impide que dos instancias del mismo bot corran a la vez. Una simulación (`--dry-run`) sí puede correr junto a PM2.
- Si `.env` no tiene los secretos, el bot sale con código 2 y PM2 no lo reinicia en bucle.

Sin PM2 también funciona: `nohup .venv/bin/python run_general.py --loop 300 >> logs/general.log 2>&1 &`

## Telegram

- **Secretos** (`.env` en el VPS, *Secrets* en GitHub): `TELEGRAM_TOKEN` y `CHAT_ID`. Para mandar cada bot a otro chat o bot, define `TELEGRAM_TOKEN_GENERAL` / `CHAT_ID_GENERAL` (o `_ALIMENTOS`); si no existen, se usan los comunes.
- Cada mensaje lleva hashtags por grupo y categoría (ej. `#PlazaVea_Alimentos #Abarrotes`, `#PlazaVea_Moda #ModaMujer`).
- **Por corrida:** `max_individual_alerts` alertas con foto, repartidas entre categorías. El resto, hasta `digest_max_items`, va en **resúmenes por categoría** de hasta 30 ofertas cada uno.
- Si los dos bots escriben al mismo grupo a la vez, pueden toparse con el límite de Telegram (~20 mensajes/min por grupo). El bot espera y reintenta solo, pero si pasa seguido conviene usar `CHAT_ID_GENERAL` para separar los chats.

## Historial con TTL

| Situación | ¿Se notifica? |
|---|---|
| Producto nuevo | ✅ |
| Su precio bajó ≥ `renotify_drop_pct` (3 %) desde el último aviso | ✅ `📉 ¡Bajó más!` |
| Mismo precio o subió | ❌ |
| Pasaron `ttl_days` (3) desde el último aviso y sigue en oferta | ✅ |

Para empezar de cero, borra `state/plazavea_<bot>.json`.

## GitHub Actions

La producción está en el VPS. **`monitor.yml` ya no está programado:**

- **Push a `main`** (cambios en código o config) → prueba de integración real de ambos bots en modo simulación: no envía nada.
- **Manual** (*Actions → Monitor Plaza Vea (pruebas) → Run workflow*) → eliges el bot. Por defecto es una simulación. Desmárcala solo si el VPS está caído: ese historial no se comparte con el del VPS.
- `ci.yml` corre los tests (`pytest`) en cada push.

## Desarrollo local

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python run_alimentos.py --dry-run -v     # una corrida simulada
```

## Estructura

```
├── run_alimentos.py / run_general.py   # puntos de entrada (aceptan --loop, --dry-run, -v)
├── config.yaml                          # configuración común
├── config.alimentos.yaml / config.general.yaml
├── ecosystem.config.js                  # PM2: ambos bots 24/7
├── deploy/setup_vps.sh                  # instalación / actualización en el VPS
├── .env.example                         # plantilla de secretos
├── tracker/
│   ├── __main__.py   # CLI
│   ├── runner.py     # modo continuo, señales, carga de .env
│   ├── config.py     # YAML con herencia (extends) + secretos
│   ├── scraper.py    # API VTEX de Plaza Vea
│   ├── pipeline.py   # filtros → historial → Telegram → resumen
│   ├── state.py      # historial con TTL + candado
│   ├── telegram.py · formatter.py · filters.py · pricing.py · models.py · http_client.py · base.py
└── tests/            # pytest (sin red)
```

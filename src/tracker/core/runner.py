"""Ejecución continua para servidores (VPS): una corrida cada N segundos, sin caerse.

Cadencia fija de inicio a inicio: con `--loop 300` cada corrida empieza 5 min después
de la anterior (si una dura 70 s, la pausa es de 230 s). Si una corrida supera el
intervalo, la siguiente empieza de inmediato.

Robustez:
  * Cualquier excepción dentro de una corrida se registra y el bucle continúa.
  * La configuración se recarga en cada ciclo; si queda inválida se sigue con la anterior.
  * SIGTERM/SIGINT (PM2, Docker, Ctrl+C) detienen el bucle limpiamente: si llega durante
    una corrida, se interrumpe guardando el historial de lo ya enviado.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
import time
from pathlib import Path
from typing import Callable

from .config import AppConfig, ConfigError, load_config

log = logging.getLogger(__name__)


class GracefulExit(BaseException):
    """Interrumpe la corrida en curso. Hereda de BaseException para que ningún
    `except Exception` intermedio (red, Telegram) la capture."""


def _valor_env(crudo: str) -> str:
    """Valor de una línea .env: quita comillas y los comentarios al final ("TOKEN=123:ABC  # bot")."""
    crudo = crudo.strip()
    if crudo[:1] in ("'", '"'):
        cierre = crudo.find(crudo[0], 1)
        return crudo[1:cierre] if cierre > 0 else crudo[1:]
    return re.split(r"\s+#", crudo, maxsplit=1)[0].strip()


def load_env_file(path: str | Path) -> int:
    """Carga KEY=VALUE de un archivo .env sin pisar variables ya definidas. Devuelve cuántas cargó."""
    ruta = Path(path)
    if not ruta.is_file():
        return 0
    cargadas = 0
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.removeprefix("export ").partition("=")
        clave, valor = clave.strip(), _valor_env(valor)
        if clave and clave not in os.environ:
            os.environ[clave] = valor
            cargadas += 1
    return cargadas


def _run_pipeline(cfg: AppConfig, dry_run: bool, max_alerts: int | None) -> int:
    from .pipeline import run

    return run(cfg, dry_run=dry_run, max_alerts=max_alerts)


def run_loop(
    config_path: str | Path,
    *,
    interval: float,
    dry_run: bool = False,
    max_alerts: int | None = None,
    stop_event: threading.Event | None = None,
    run_once: Callable[[AppConfig, bool, int | None], int] = _run_pipeline,
    monotonic: Callable[[], float] = time.monotonic,
    max_cycles: int | None = None,
    install_signals: bool = True,
) -> int:
    stop = stop_event or threading.Event()
    corriendo = threading.Event()

    if install_signals:
        def _al_recibir_senal(signum, _frame):
            if stop.is_set():          # segunda señal: salida inmediata
                raise SystemExit(1)
            log.info("Señal %s recibida: deteniendo el bucle…", signal.Signals(signum).name)
            stop.set()
            if corriendo.is_set():
                raise GracefulExit()

        signal.signal(signal.SIGTERM, _al_recibir_senal)
        signal.signal(signal.SIGINT, _al_recibir_senal)

    cfg: AppConfig | None = None
    ciclo = 0
    log.info("Modo continuo: una corrida cada %.0f s (%s).", interval, Path(config_path).name)
    while not stop.is_set():
        ciclo += 1
        inicio = monotonic()

        try:
            cfg = load_config(config_path, require_secrets=not dry_run)
        except ConfigError as exc:
            if cfg is None:
                log.error("Configuración inválida, no se puede iniciar: %s", exc)
                return 2
            log.error("Configuración inválida (%s); se usa la última válida.", exc)

        try:
            corriendo.set()
            log.info("── Ciclo %s · %s ──", ciclo, cfg.display_name)
            run_once(cfg, dry_run, max_alerts)
        except GracefulExit:
            log.info("Corrida interrumpida por señal; historial guardado.")
            break
        except Exception:  # noqa: BLE001 — el bucle nunca debe caerse
            log.exception("Error inesperado en el ciclo %s; se reintenta en el siguiente.", ciclo)
        finally:
            corriendo.clear()

        if max_cycles and ciclo >= max_cycles:
            break
        duracion = monotonic() - inicio
        espera = interval - duracion
        if espera <= 0:
            log.warning("La corrida duró %.0f s (más que el intervalo de %.0f s); la siguiente empieza ya.",
                        duracion, interval)
            espera = 0.0
        else:
            log.info("Corrida de %.0f s. Próxima en %.0f s.", duracion, espera)
        if stop.wait(espera):
            break
    log.info("Bucle detenido tras %s ciclo(s).", ciclo)
    return 0

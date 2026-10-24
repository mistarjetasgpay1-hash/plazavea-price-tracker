"""CLI: python -m tracker --config ARCHIVO [--loop SEGUNDOS] [--dry-run] [--max-alerts N] [-v]"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .core.config import ConfigError, load_config
from .core.runner import load_env_file, run_loop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracker", description="Monitor de ofertas con alertas por Telegram")
    parser.add_argument("--config", default="config.yaml", help="Ruta al archivo YAML del bot")
    parser.add_argument("--loop", type=float, metavar="SEGUNDOS", default=None,
                        help="Modo continuo: una corrida cada N segundos (ej. 300). Sin este flag corre una vez.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extrae y filtra, pero no envía a Telegram ni guarda el historial")
    parser.add_argument("--max-alerts", type=int, default=None, help="Límite de alertas con foto por corrida")
    parser.add_argument("--env-file", default=None,
                        help="Archivo .env con los secretos (default: .env junto al archivo de configuración)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log detallado")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,   # PM2/Docker guardan stdout en el log principal
    )
    for ruidoso in ("urllib3",):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
    log = logging.getLogger("tracker")

    env_file = Path(args.env_file) if args.env_file else Path(args.config).resolve().parent / ".env"
    if load_env_file(env_file):
        log.info("Variables cargadas desde %s", env_file)

    dry_run = args.dry_run or os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}

    if args.loop is not None:
        if args.loop < 30:
            log.error("--loop debe ser de al menos 30 segundos.")
            return 2
        return run_loop(args.config, interval=args.loop, dry_run=dry_run, max_alerts=args.max_alerts)

    try:
        cfg = load_config(args.config, require_secrets=not dry_run)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    from .core.pipeline import run

    return run(cfg, dry_run=dry_run, max_alerts=args.max_alerts)


if __name__ == "__main__":
    sys.exit(main())

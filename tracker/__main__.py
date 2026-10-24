"""CLI: python -m tracker [--config config.yaml] [--dry-run] [--max-alerts N] [-v]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracker", description="Monitor de ofertas con alertas por Telegram")
    parser.add_argument("--config", default="config.yaml", help="Ruta al archivo YAML (default: config.yaml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extrae y filtra, pero no envía a Telegram ni guarda el historial")
    parser.add_argument("--max-alerts", type=int, default=None, help="Límite de alertas con foto en esta corrida")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log detallado")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for ruidoso in ("urllib3", "DrissionPage"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)

    try:
        cfg = load_config(args.config, require_secrets=not dry_run)
    except ConfigError as exc:
        logging.getLogger("tracker").error("%s", exc)
        return 2

    from .pipeline import run

    return run(cfg, dry_run=dry_run, max_alerts=args.max_alerts)


if __name__ == "__main__":
    sys.exit(main())

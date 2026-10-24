#!/usr/bin/env python3
"""Bot Plaza Vea · Alimentos y consumo diario.

    python run_alimentos.py               # una corrida
    python run_alimentos.py --loop 300    # continuo: una corrida cada 5 minutos (VPS)
    python run_alimentos.py --dry-run -v  # simulación, sin enviar ni guardar

Configuración: config.alimentos.yaml (hereda de config.yaml). Secretos: archivo .env.
Cambios propios del VPS: config.alimentos.local.yaml (no se sube a GitHub; empieza con
`extends: config.alimentos.yaml`). Si existe, se usa en lugar de config.alimentos.yaml.
"""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "src"))

from tracker.__main__ import main  # noqa: E402

CONFIG = RAIZ / "config.alimentos.yaml"
CONFIG_LOCAL = RAIZ / "config.alimentos.local.yaml"

if __name__ == "__main__":
    config = CONFIG_LOCAL if CONFIG_LOCAL.exists() else CONFIG
    sys.exit(main(["--config", str(config), *sys.argv[1:]]))

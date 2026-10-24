#!/usr/bin/env python3
"""Bot Plaza Vea · Alimentos y consumo diario.

    python run_alimentos.py               # una corrida
    python run_alimentos.py --loop 300    # continuo: una corrida cada 5 minutos (VPS)
    python run_alimentos.py --dry-run -v  # simulación, sin enviar ni guardar

Configuración: config.alimentos.yaml (hereda de config.yaml). Secretos: archivo .env.
"""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from tracker.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["--config", str(RAIZ / "config.alimentos.yaml"), *sys.argv[1:]]))

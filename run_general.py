#!/usr/bin/env python3
"""Bot Plaza Vea · General (tecnología, electro, juguetes, moda, hogar…).

    python run_general.py               # una corrida
    python run_general.py --loop 300    # continuo: una corrida cada 5 minutos (VPS)
    python run_general.py --dry-run -v  # simulación, sin enviar ni guardar

Configuración: config.general.yaml (hereda de config.yaml). Secretos: archivo .env.
"""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from tracker.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["--config", str(RAIZ / "config.general.yaml"), *sys.argv[1:]]))

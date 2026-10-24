#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Instala / actualiza los bots de Plaza Vea en un VPS Linux y los deja 24/7 con PM2.
#
#  Primera vez:
#    git clone https://github.com/mistarjetasgpay1-hash/plazavea-price-tracker.git
#    cd plazavea-price-tracker && bash deploy/setup_vps.sh
#    (crea .env → edítalo con tus secretos → vuelve a ejecutar el script)
#
#  Actualizar después de cambios en GitHub:  bash deploy/setup_vps.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

paso() { printf '\n==> %s\n' "$1"; }

paso "Requisitos"
command -v python3 >/dev/null || { echo "Falta Python: sudo apt install -y python3 python3-venv"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Se requiere Python 3.11 o superior (tienes $(python3 -V))."; exit 1; }
python3 -c 'import venv' 2>/dev/null || { echo "Falta venv: sudo apt install -y python3-venv"; exit 1; }
command -v node >/dev/null || { echo "Falta Node.js (para PM2): sudo apt install -y nodejs npm"; exit 1; }
if ! command -v pm2 >/dev/null; then
  echo "Instalando PM2…"
  npm install -g pm2 >/dev/null 2>&1 || sudo npm install -g pm2
fi

paso "Código"
git pull --ff-only 2>/dev/null || echo "(sin git pull: hay cambios locales o no hay remoto)"

paso "Entorno virtual y dependencias"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check --upgrade pip
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

paso "Secretos (.env)"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Se creó .env. Complétalo con tu TELEGRAM_TOKEN y CHAT_ID:  nano .env"
  echo "Luego vuelve a ejecutar:  bash deploy/setup_vps.sh"
  exit 0
fi
chmod 600 .env
grep -qE '^TELEGRAM_TOKEN=.+' .env && ! grep -q 'TU_TOKEN' .env \
  || { echo "Completa TELEGRAM_TOKEN y CHAT_ID en .env antes de continuar."; exit 1; }

paso "Historial"
mkdir -p state logs
for bot in alimentos general; do
  archivo="state/plazavea_${bot}.json"
  [ -f "$archivo" ] && { echo "$archivo ya existe"; continue; }
  # Migra el historial que dejó GitHub Actions para no reenviar ofertas ya avisadas.
  if git fetch --quiet origin state 2>/dev/null && git show FETCH_HEAD:plazavea.json > "$archivo" 2>/dev/null; then
    echo "$archivo migrado desde la rama state"
  else
    rm -f "$archivo"
    echo "$archivo se creará en la primera corrida"
  fi
done

paso "Prueba (simulación de una corrida del bot de alimentos, ~1 min)"
if .venv/bin/python run_alimentos.py --dry-run > logs/prueba_instalacion.log 2>&1; then
  resumen=$(grep -E "Resumen" logs/prueba_instalacion.log | tail -1 || true)
  echo "${resumen#*: }"
  if echo "$resumen" | grep -q "páginas ok=0 "; then
    echo "⚠️  El VPS no pudo leer ninguna página de Plaza Vea (¿firewall o IP bloqueada?)."
    echo "    Revisa logs/prueba_instalacion.log. Los bots se inician igual y avisarán por Telegram si sigue fallando."
  fi
else
  echo "⚠️  La simulación falló. Revisa logs/prueba_instalacion.log"; exit 1
fi

paso "Iniciando con PM2"
pm2 startOrReload ecosystem.config.js --update-env
pm2 save
pm2 describe pm2-logrotate >/dev/null 2>&1 || pm2 install pm2-logrotate >/dev/null 2>&1 || true

cat <<'MSG'

Listo: plazavea-alimentos y plazavea-general corren cada 5 minutos.

  pm2 status                    estado
  pm2 logs plazavea-general     logs en vivo
  pm2 restart all               reiniciar (tras editar config*.yaml o .env)

Para que arranquen solos si el VPS se reinicia, ejecuta UNA vez:
  pm2 startup        (y copia/pega el comando sudo que te muestre)
MSG

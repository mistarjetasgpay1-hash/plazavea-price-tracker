// ─────────────────────────────────────────────────────────────────────────────
//  PM2 — los dos bots de Plaza Vea corriendo 24/7 en el VPS
//
//    pm2 start ecosystem.config.js     # iniciar (o: bash deploy/setup_vps.sh)
//    pm2 status                        # estado de ambos bots
//    pm2 logs plazavea-general         # ver logs en vivo
//    pm2 restart all | pm2 stop all    # reiniciar / detener
//    pm2 save && pm2 startup           # que arranquen solos al reiniciar el VPS
//
//  Intervalo: 300 s por defecto. Para cambiarlo: LOOP_SECONDS=240 pm2 restart all --update-env
// ─────────────────────────────────────────────────────────────────────────────
const path = require("path");

const PYTHON = path.join(__dirname, ".venv", "bin", "python");
const LOOP_SECONDS = process.env.LOOP_SECONDS || "300";

const bot = (name, script) => ({
  name,
  script,
  interpreter: PYTHON,
  args: `--loop ${LOOP_SECONDS}`,
  cwd: __dirname,
  autorestart: true,               // si el proceso muere, PM2 lo vuelve a levantar
  exp_backoff_restart_delay: 5000, // reinicios con espera creciente si falla repetidamente
  min_uptime: "60s",
  max_restarts: 50,
  stop_exit_codes: [2],            // 2 = configuración/secretos inválidos: no reiniciar en bucle
  kill_timeout: 15000,             // margen para cerrar limpio y guardar el historial
  max_memory_restart: "300M",
  merge_logs: true,
  out_file: path.join(__dirname, "logs", `${name}.log`),
  error_file: path.join(__dirname, "logs", `${name}.error.log`),
  env: { PYTHONUNBUFFERED: "1" },
});

module.exports = {
  apps: [
    bot("plazavea-alimentos", "run_alimentos.py"),
    bot("plazavea-general", "run_general.py"),
  ],
};

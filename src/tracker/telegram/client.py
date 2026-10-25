"""Despachador de Telegram con throttle por chat y manejo de límites (HTTP 429).

Si Telegram pide esperar más de `max_wait_s` (o se agota `wait_budget_s` esperando en una
corrida), el cliente se pausa: los envíos restantes devuelven False sin llamar a la API y
quedan pendientes para la próxima corrida. Así un límite largo no congela el bot.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        min_interval_s: float = 3.2,
        send_photos: bool = True,
        dry_run: bool = False,
        session: requests.Session | None = None,
        timeout_s: float = 20.0,
        max_wait_s: float = 30.0,
        wait_budget_s: float = 90.0,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.min_interval_s = min_interval_s
        self.send_photos = send_photos
        self.dry_run = dry_run
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        self.max_wait_s = max_wait_s          # espera máxima aceptada en un 429
        self.wait_budget_s = wait_budget_s    # espera total por 429 aceptada en una corrida
        self._last_send = 0.0
        self._waited_s = 0.0
        self.paused = False                   # Telegram pidió esperar demasiado: no más envíos en esta corrida
        self.sent = 0
        self.last_error = ""
        # Diagnóstico (logs de GitHub Actions): respuestas HTTP reales de la API de Telegram.
        self.http_calls = 0                   # peticiones que llegaron a salir hacia Telegram
        self.http_status: dict[int, int] = {} # código HTTP → veces
        self.network_errors = 0               # peticiones sin respuesta (timeout, DNS, conexión)
        self.simulated = 0                    # llamadas no enviadas por DRY_RUN
        self.skipped_paused = 0               # llamadas omitidas por pausa (429 largo)

    @property
    def available(self) -> bool:
        return self.dry_run or not self.paused

    # ------------------------------------------------------------------ núcleo
    def _throttle(self) -> None:
        espera = self.min_interval_s - (time.monotonic() - self._last_send)
        if self._last_send and espera > 0:
            time.sleep(espera)

    def _pause(self, motivo: str) -> None:
        self.paused = True
        self.last_error = motivo
        log.warning("%s Los envíos restantes quedan para la próxima corrida.", motivo)

    @staticmethod
    def _payload(resp) -> dict:
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _log_response(self, method: str, resp, payload: dict, intento: int) -> None:
        """Una línea por respuesta de Telegram: método, código HTTP y lo que contestó la API."""
        self.http_status[resp.status_code] = self.http_status.get(resp.status_code, 0) + 1
        if resp.ok:
            resultado = payload.get("result")
            mensaje = resultado.get("message_id") if isinstance(resultado, dict) else None
            log.info("[TG] ← %s · HTTP %s · ok=%s%s", method, resp.status_code,
                     str(payload.get("ok", True)).lower(), f" · message_id={mensaje}" if mensaje else "")
        else:
            descripcion = str(payload.get("description") or getattr(resp, "text", "") or "")[:200]
            log.warning("[TG] ← %s · HTTP %s · ok=false · error_code=%s · %s (intento %s/4)", method,
                        resp.status_code, payload.get("error_code", resp.status_code), descripcion, intento)

    def _call(self, method: str, data: dict, files: dict | None = None) -> bool:
        if self.dry_run:
            self.simulated += 1
            log.info("[TG] %s NO invocado (DRY_RUN: simulación) → %s", method,
                     (data.get("caption") or data.get("text") or "")[:120].replace("\n", " | "))
            self.sent += 1
            return True
        if self.paused:
            self.skipped_paused += 1
            log.info("[TG] %s omitido: Telegram en pausa (%s).", method, self.last_error)
            return False

        url = API_URL.format(token=self.token, method=method)   # el token nunca se escribe en el log
        for intento in range(1, 5):
            self._throttle()
            self.http_calls += 1
            try:
                resp = self.session.post(url, data=data, files=files, timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_send = time.monotonic()
                self.network_errors += 1
                self.last_error = f"red: {type(exc).__name__}"
                log.warning("[TG] ← %s · sin respuesta HTTP (%s, intento %s/4)", method, type(exc).__name__, intento)
                time.sleep(2 * intento)
                continue
            self._last_send = time.monotonic()
            payload = self._payload(resp)
            self._log_response(method, resp, payload, intento)

            if resp.ok:
                self.sent += 1
                return True
            descripcion = str(payload.get("description") or resp.text)[:200]

            if resp.status_code == 429:
                try:
                    espera = float((payload.get("parameters") or {}).get("retry_after") or 5)
                except (TypeError, ValueError):
                    espera = 5.0
                if espera > self.max_wait_s or self._waited_s + espera > self.wait_budget_s:
                    self._pause(f"Telegram pidió esperar {espera:.0f}s (429).")
                    return False
                log.warning("Telegram pidió esperar %.0fs (429).", espera)
                self.last_error = f"429: esperar {espera:.0f}s"
                self._waited_s += espera
                time.sleep(espera + 0.5)
                continue
            if resp.status_code >= 500:
                self.last_error = f"HTTP {resp.status_code}: {descripcion}"
                time.sleep(2 * intento)
                continue
            # 400/403: foto inválida, chat incorrecto, bot expulsado... no se reintenta.
            self.last_error = f"HTTP {resp.status_code}: {descripcion}"
            return False
        return False

    # ------------------------------------------------------------ diagnóstico
    def check_connection(self) -> bool | None:
        """Verifica token (getMe) y chat (getChat) antes de la corrida y registra la respuesta HTTP.

        True  → token y chat válidos.
        False → Telegram rechazó el token o el chat (401/400/403/404): ningún envío funcionará.
        None  → no se pudo comprobar (simulación, red caída o Telegram 5xx): la corrida sigue.
        """
        if self.dry_run:
            log.info("[TG] Verificación de token/chat omitida (DRY_RUN: simulación).")
            return None
        if not self.token or not self.chat_id:
            self.last_error = "token o chat_id vacío"
            log.error("[TG] Token o chat_id vacío: revisa los Secrets del repositorio.")
            return False
        for method, data in (("getMe", {}), ("getChat", {"chat_id": self.chat_id})):
            try:
                resp = self.session.post(API_URL.format(token=self.token, method=method), data=data,
                                         timeout=self.timeout_s)
            except requests.RequestException as exc:
                log.warning("[TG] ← %s · sin respuesta HTTP (%s): no se pudo verificar.", method, type(exc).__name__)
                return None
            payload = self._payload(resp)
            resultado = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            if resp.ok:
                if method == "getMe":
                    log.info("[TG] ← getMe · HTTP %s · bot @%s (id %s)", resp.status_code,
                             resultado.get("username", "?"), resultado.get("id", "?"))
                else:
                    log.info("[TG] ← getChat · HTTP %s · chat \"%s\" (tipo %s)", resp.status_code,
                             resultado.get("title") or resultado.get("username") or resultado.get("first_name") or "?",
                             resultado.get("type", "?"))
                continue
            descripcion = str(payload.get("description") or getattr(resp, "text", "") or "")[:200]
            log.error("[TG] ← %s · HTTP %s · ok=false · %s", method, resp.status_code, descripcion)
            if resp.status_code >= 500 or resp.status_code == 429:
                return None
            self.last_error = f"{method} HTTP {resp.status_code}: {descripcion}"
            return False
        return True

    def api_summary(self) -> str:
        """Resumen de lo que respondió Telegram en la corrida (para el log y el resumen de Actions)."""
        if self.dry_run:
            return f"0 llamadas reales (DRY_RUN: {self.simulated} simuladas)"
        codigos = ", ".join(f"HTTP {c}: {n}" for c, n in sorted(self.http_status.items())) or "ninguna respuesta"
        extra = []
        if self.network_errors:
            extra.append(f"sin respuesta: {self.network_errors}")
        if self.skipped_paused:
            extra.append(f"omitidas por pausa: {self.skipped_paused}")
        if self.last_error:
            extra.append(f"último error: {self.last_error}")
        return f"{self.http_calls} llamadas · {codigos}" + (f" · {' · '.join(extra)}" if extra else "")

    def _base(self, thread_id: int | None, silent: bool) -> dict:
        data: dict = {"chat_id": self.chat_id, "parse_mode": "HTML"}
        if thread_id:
            data["message_thread_id"] = thread_id
        if silent:
            data["disable_notification"] = "true"
        return data

    # --------------------------------------------------------------------- API
    def send_message(self, text: str, *, thread_id: int | None = None, silent: bool = False,
                     preview: bool = True) -> bool:
        data = self._base(thread_id, silent)
        data["text"] = text  # el formatter garantiza ≤ 4096 caracteres visibles (no se corta el HTML)
        data["link_preview_options"] = json.dumps({"is_disabled": not preview})
        return self._call("sendMessage", data)

    def send_photo(self, photo: str | bytes, caption: str, *, thread_id: int | None = None) -> bool:
        data = self._base(thread_id, silent=False)
        data["caption"] = caption[:1024]
        if isinstance(photo, bytes):
            return self._call("sendPhoto", data, files={"photo": ("oferta.jpg", photo, "image/jpeg")})
        data["photo"] = photo
        return self._call("sendPhoto", data)

    def send_offer(
        self,
        caption: str,
        *,
        photo_url: str = "",
        thread_id: int | None = None,
        photo_loader: Callable[[], bytes | None] | None = None,
    ) -> bool:
        """Foto por URL → foto descargada → solo texto (en ese orden)."""
        if self.send_photos and photo_url:
            if self.send_photo(photo_url, caption, thread_id=thread_id):
                return True
            if not self.available:
                return False
            if photo_loader and not self.dry_run:
                contenido = photo_loader()
                if contenido and self.send_photo(contenido, caption, thread_id=thread_id):
                    return True
                if not self.available:
                    return False
        return self.send_message(caption, thread_id=thread_id, preview=True)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass

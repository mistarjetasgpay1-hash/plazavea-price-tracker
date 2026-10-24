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

    def _call(self, method: str, data: dict, files: dict | None = None) -> bool:
        if self.dry_run:
            log.info("[DRY-RUN] %s → %s", method, (data.get("caption") or data.get("text") or "")[:120].replace("\n", " | "))
            self.sent += 1
            return True
        if self.paused:
            return False

        url = API_URL.format(token=self.token, method=method)
        for intento in range(1, 5):
            self._throttle()
            try:
                resp = self.session.post(url, data=data, files=files, timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_send = time.monotonic()
                self.last_error = f"red: {type(exc).__name__}"
                log.warning("Error de red con Telegram (%s/4): %s", intento, type(exc).__name__)
                time.sleep(2 * intento)
                continue
            self._last_send = time.monotonic()

            if resp.ok:
                self.sent += 1
                return True
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            payload = payload if isinstance(payload, dict) else {}
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
            self.last_error = descripcion
            log.warning("Telegram %s devolvió %s: %s", method, resp.status_code, descripcion)
            return False
        return False

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

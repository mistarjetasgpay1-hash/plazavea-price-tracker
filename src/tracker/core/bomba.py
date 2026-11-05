"""Canal de ofertas "bomba" (bot 4): reenvía a otro bot/chat las alertas con descuento > 69 %.

Cómo funciona:
  * El envío normal no cambia. Solo cuando el canal habitual ACEPTA una oferta (y queda en el
    historial) se reenvía aquí la misma alerta. Así el historial de siempre (TTL de 3 días +
    re-aviso si baja ≥3 %) evita repetir la misma oferta en este canal, y una oferta nueva llega
    a los dos. Las que en el canal habitual salen dentro de un resumen llegan aquí con su foto.
  * El umbral se compara con el % entero que muestra el mensaje (redondeado hacia abajo, como la
    web): con `discount_gt: 69` se reenvía desde "-70 %"; un "-69 %" (p. ej. 69,8 %) no.
  * Si TELEGRAM_TOKEN4 / CHAT_ID4 faltan, o Telegram los rechaza, el reenvío se desactiva en la
    corrida y el bot sigue normal: ningún fallo de este canal detiene la corrida ni la marca en rojo.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..telegram import formatter
from ..telegram.client import TelegramNotifier
from .config import AppConfig, _names, env_source
from .models import Offer

log = logging.getLogger(__name__)

LABEL = "TG4"   # prefijo de las respuestas HTTP del bot 4 en el log ("[TG4] ← sendPhoto · HTTP 200 …")


class BombaForwarder:
    def __init__(self, cfg: AppConfig, *, dry_run: bool = False, notifier=None) -> None:
        b = cfg.bomba
        self.threshold = float(b.discount_gt)
        self.dry_run = dry_run
        self.display_name = cfg.display_name
        self.token_env, self.chat_id_env = b.token_env, b.chat_id_env
        self.token_len = len(b.token)
        self.has_secrets = bool(b.token and b.chat_id)
        self.sent = 0
        self.failed = 0
        self.skipped = 0
        self.last_error = ""
        self.disabled_reason = ""
        self._keys: set[str] = set()      # ya reenviadas en esta corrida (nunca dos veces la misma)
        self._verified = False
        if not b.enabled:
            self.disabled_reason = "desactivado en la configuración (bomba.enabled: false)"
        elif not self.has_secrets and not dry_run:
            faltan = [_names(n) for n, v in ((b.token_env, b.token), (b.chat_id_env, b.chat_id)) if not v]
            self.disabled_reason = f"{' y '.join(faltan)} vacío(s)"
        self.notifier = None
        if not self.disabled_reason:
            self.notifier = notifier or TelegramNotifier(
                b.token,
                b.chat_id,
                min_interval_s=cfg.telegram.min_interval_s,
                send_photos=cfg.telegram.send_photos,
                dry_run=dry_run,
                label=LABEL,
            )

    # ------------------------------------------------------------------ estado
    @property
    def active(self) -> bool:
        return self.notifier is not None and not self.disabled_reason

    def is_bomba(self, offer: Offer) -> bool:
        """True si el % que encabeza el mensaje es estrictamente mayor que el umbral."""
        return formatter.pct(offer.headline_discount) > self.threshold

    def wants(self, offer: Offer) -> bool:
        return self.active and offer.key not in self._keys and self.is_bomba(offer)

    def log_status(self) -> None:
        """Una línea al inicio de la corrida: si el bot 4 está activo y de qué variables leyó."""
        umbral = f"> {self.threshold:g} %"
        if not self.active:
            log.info("[BOMBA] Bot 4 (ofertas %s) desactivado: %s. El bot sigue normal sin reenviar.",
                     umbral, self.disabled_reason)
        elif not self.has_secrets:
            log.info("[BOMBA] Bot 4 (ofertas %s) en SIMULACIÓN: %s / %s vacíos; solo se muestra qué se reenviaría.",
                     umbral, _names(self.token_env), _names(self.chat_id_env))
        else:
            log.info("[BOMBA] Bot 4 activo: reenvía las alertas %s · token: variable %s (definida, %s caracteres) · "
                     "chat_id: variable %s (definida)%s", umbral, env_source(self.token_env) or _names(self.token_env),
                     self.token_len, env_source(self.chat_id_env) or _names(self.chat_id_env),
                     " [SIMULACIÓN, no sale nada]" if self.dry_run else "")

    def _verify(self) -> None:
        """getMe/getChat del bot 4 antes del primer reenvío (solo si hay ofertas bomba en la corrida)."""
        self._verified = True
        verificar = getattr(self.notifier, "check_connection", None)
        if verificar is None:
            return
        try:
            resultado = verificar()
        except Exception as exc:  # noqa: BLE001 — el canal extra nunca detiene la corrida
            log.warning("[BOMBA] No se pudo verificar el bot 4 (%s); se intenta enviar igual.", type(exc).__name__)
            return
        if resultado is False:
            self.last_error = str(getattr(self.notifier, "last_error", "") or "")
            self.disabled_reason = f"Telegram rechazó el token o el chat del bot 4 ({self.last_error})"
            from .pipeline import annotate   # import diferido: pipeline importa este módulo

            annotate("warning", f"{self.display_name}: {self.disabled_reason}. La corrida sigue sin reenviar "
                                f"ofertas bomba; revisa los Secrets {_names(self.token_env)} / "
                                f"{_names(self.chat_id_env)} y que el bot sea miembro (o admin) del chat.")

    # ------------------------------------------------------------------- envío
    def forward(self, offer: Offer, caption: str, *, photo_loader: Callable[[], bytes | None] | None = None) -> bool:
        """Reenvía al bot 4 la alerta ya aceptada por el canal habitual. Nunca lanza excepciones."""
        if not self.wants(offer):
            return False
        if not self._verified:
            self._verify()
            if not self.active:
                return False
        self._keys.add(offer.key)
        dto = formatter.pct(offer.headline_discount)
        if not getattr(self.notifier, "available", True):
            self.skipped += 1
            log.info("[BOMBA] Bot 4 en pausa (%s): no se reenvía -%s%% %s.",
                     getattr(self.notifier, "last_error", ""), dto, offer.name[:60])
            return False
        try:
            ok = bool(self.notifier.send_offer(caption, photo_url=offer.image_url, photo_loader=photo_loader))
            error = str(getattr(self.notifier, "last_error", "") or "")
        except Exception as exc:  # noqa: BLE001 — el canal extra nunca detiene la corrida
            ok, error = False, f"{type(exc).__name__}: {exc}"
        if ok:
            self.sent += 1
            log.info("[BOMBA] ✓ Reenviada al bot 4%s: -%s%% %s", " [SIMULACIÓN]" if self.dry_run else "",
                     dto, offer.name[:60])
        else:
            self.failed += 1
            self.last_error = error
            log.warning("[BOMBA] ✗ El bot 4 no aceptó -%s%% %s (%s)", dto, offer.name[:60], error or "sin detalle")
            if error.startswith(("HTTP 401", "HTTP 403")):
                # Token revocado o bot bloqueado/expulsado: los siguientes reenvíos fallarían igual.
                self.disabled_reason = f"Telegram rechazó el bot 4 ({error})"
                log.warning("[BOMBA] Bot 4 desactivado por el resto de la corrida: %s.", self.disabled_reason)
        return ok

    # --------------------------------------------------------------- resumen
    def summary(self) -> str:
        if self.notifier is None:
            return f"desactivado: {self.disabled_reason}"
        estado = "activo" if self.active else f"desactivado: {self.disabled_reason}"
        partes = [estado, f"{self.sent} reenviadas", f"{self.failed} fallidas"]
        if self.skipped:
            partes.append(f"{self.skipped} omitidas por pausa")
        api = self.notifier.api_summary() if hasattr(self.notifier, "api_summary") else "—"
        return " · ".join(partes) + f" · API: {api}"

    def close(self) -> None:
        if self.notifier is not None and hasattr(self.notifier, "close"):
            try:
                self.notifier.close()
            except Exception:  # noqa: BLE001
                pass

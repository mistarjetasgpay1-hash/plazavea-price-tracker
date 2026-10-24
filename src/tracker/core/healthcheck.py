"""Ping a un monitor externo de "estoy vivo" (healthchecks.io o compatible).

Al terminar cada corrida real se hace POST a la URL (éxito) o a URL/fail (fallo), con el
resumen de la corrida en el cuerpo. Si el monitor deja de recibir pings —VPS caído, PM2
detenido, proceso colgado— avisa por su cuenta. Nunca interrumpe al bot si falla.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


def ping(url: str, *, ok: bool, body: str = "", timeout_s: float = 10.0) -> bool:
    if not url:
        return False
    destino = url.rstrip("/") + ("" if ok else "/fail")
    try:
        resp = requests.post(destino, data=body.encode("utf-8")[:10_000], timeout=timeout_s)
    except requests.RequestException as exc:
        log.warning("No se pudo avisar al monitor externo (healthcheck): %s", type(exc).__name__)
        return False
    if not resp.ok:
        log.warning("El monitor externo (healthcheck) respondió HTTP %s.", resp.status_code)
    return resp.ok

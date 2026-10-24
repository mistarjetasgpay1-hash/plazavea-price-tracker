"""Cliente HTTP con reintentos, backoff exponencial con jitter y pausas corteses."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from .config import HttpConfig

log = logging.getLogger(__name__)

# Un User-Agent realista por sesión. Rotarlo en cada petición es una señal de bot,
# así que se elige uno al iniciar y se mantiene durante toda la corrida.
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
)

BASE_HEADERS = {
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.6",
}


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BlockedError(HttpError):
    """La tienda devolvió una página de bloqueo (403/401) o contenido no esperado."""


class HttpClient:
    """Envoltura mínima sobre `requests` o `curl_cffi` con una política común de reintentos."""

    def __init__(
        self,
        cfg: HttpConfig,
        *,
        backend: str = "requests",
        impersonate: str = "chrome",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.backend = backend
        if backend == "curl_cffi":
            from curl_cffi import requests as cffi_requests

            # curl_cffi imita la huella TLS/HTTP2 de Chrome y fija su propio User-Agent.
            self.session = cffi_requests.Session(impersonate=impersonate)
        elif backend == "requests":
            import requests

            self.session = requests.Session()
            self.session.headers["User-Agent"] = random.choice(USER_AGENTS)
        else:
            raise ValueError(f"Backend HTTP desconocido: {backend}")
        self.session.headers.update(BASE_HEADERS)
        if headers:
            self.session.headers.update(headers)
        self._last_request = 0.0
        self.requests_made = 0

    # ------------------------------------------------------------------ helpers
    def _polite_wait(self) -> None:
        objetivo = random.uniform(self.cfg.delay_min_s, self.cfg.delay_max_s)
        transcurrido = time.monotonic() - self._last_request
        if self._last_request and transcurrido < objetivo:
            time.sleep(objetivo - transcurrido)

    def _backoff(self, intento: int, retry_after: str | None = None) -> None:
        espera = None
        if retry_after:
            try:
                espera = float(retry_after)
            except ValueError:
                espera = None
        if espera is None:
            espera = self.cfg.backoff_base_s * (2 ** (intento - 1)) + random.uniform(0, 1)
        espera = min(espera, 30.0)
        log.debug("Reintentando en %.1fs", espera)
        time.sleep(espera)

    # ---------------------------------------------------------------------- API
    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        expect: str = "text",
        polite: bool = True,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET con reintentos. `expect` = 'text' | 'json' | 'bytes'."""
        ultimo_error = "sin intentos"
        for intento in range(1, self.cfg.retries + 1):
            if polite:
                self._polite_wait()
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.cfg.timeout_s)
            except Exception as exc:  # errores de red de requests o curl_cffi
                ultimo_error = f"{type(exc).__name__}: {exc}"
                log.warning("Error de red (%s/%s) en %s: %s", intento, self.cfg.retries, url, ultimo_error)
                self._last_request = time.monotonic()
                self._backoff(intento)
                continue
            self._last_request = time.monotonic()
            self.requests_made += 1

            status = resp.status_code
            if status == 200:
                if expect == "json":
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise BlockedError(f"Respuesta no JSON desde {url}", status) from exc
                if expect == "bytes":
                    return resp.content
                return resp.text
            if status in (401, 403):
                raise BlockedError(f"HTTP {status} (bloqueo) en {url}", status)
            if status == 429 or status >= 500:
                ultimo_error = f"HTTP {status}"
                log.warning("%s (%s/%s) en %s", ultimo_error, intento, self.cfg.retries, url)
                self._backoff(intento, resp.headers.get("Retry-After"))
                continue
            raise HttpError(f"HTTP {status} en {url}", status)
        raise HttpError(f"Reintentos agotados en {url}: {ultimo_error}")

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

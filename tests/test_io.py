"""Pruebas de los adaptadores de E/S: Telegram y cliente HTTP (sin red)."""

from __future__ import annotations

import pytest
import requests

from tracker.core.config import HttpConfig
from tracker.core.http_client import BlockedError, HttpClient, HttpError
from tracker.telegram.client import TelegramNotifier


class Resp:
    def __init__(self, status=200, payload=None, text="", headers=None, content=b""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []
        self.headers = {}

    def _siguiente(self, *a, **kw):
        self.llamadas.append((a, kw))
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    post = get = _siguiente

    def close(self):
        pass


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


# ---------------------------------------------------------------- Telegram
def test_telegram_retries_on_429_then_succeeds():
    s = FakeSession([Resp(429, {"parameters": {"retry_after": 3}}), Resp(200, {"ok": True})])
    tg = TelegramNotifier("T", "C", min_interval_s=0, session=s)
    assert tg.send_message("hola", thread_id=5)
    datos = s.llamadas[-1][1]["data"]
    assert datos["message_thread_id"] == 5 and datos["parse_mode"] == "HTML"


def test_telegram_photo_falls_back_to_bytes_then_text():
    s = FakeSession([Resp(400, {"description": "wrong file identifier"}),   # foto por URL
                     Resp(400, {"description": "bad image"}),               # foto descargada
                     Resp(200, {"ok": True})])                               # texto
    tg = TelegramNotifier("T", "C", min_interval_s=0, session=s)
    assert tg.send_offer("cap", photo_url="https://img", photo_loader=lambda: b"\xff\xd8\xffJPEG")
    metodos = [a[0].rsplit("/", 1)[-1] for a, _ in s.llamadas]
    assert metodos == ["sendPhoto", "sendPhoto", "sendMessage"]
    assert "files" in s.llamadas[1][1] and s.llamadas[1][1]["files"]


def test_telegram_does_not_retry_on_400():
    s = FakeSession([Resp(400, {"description": "chat not found"})])
    tg = TelegramNotifier("T", "C", min_interval_s=0, session=s)
    assert not tg.send_message("x") and "chat not found" in tg.last_error


def test_telegram_dry_run_never_calls_api():
    s = FakeSession([])
    tg = TelegramNotifier("", "", dry_run=True, session=s)
    assert tg.send_offer("cap", photo_url="https://img") and s.llamadas == []


# -------------------------------------------------------------------- HTTP
def _client(respuestas, retries=3):
    c = HttpClient(HttpConfig(retries=retries, delay_min_s=0, delay_max_s=0, backoff_base_s=0))
    c.session = FakeSession(respuestas)
    return c


def test_http_retries_5xx_and_network_errors():
    c = _client([Resp(503), requests.ConnectionError("boom"), Resp(200, {"ok": 1})])
    assert c.get("https://x", expect="json") == {"ok": 1}
    assert len(c.session.llamadas) == 3


def test_http_blocked_is_not_retried():
    c = _client([Resp(403)])
    with pytest.raises(BlockedError):
        c.get("https://x")
    assert len(c.session.llamadas) == 1


def test_http_gives_up_after_retries():
    c = _client([Resp(500), Resp(500)], retries=2)
    with pytest.raises(HttpError, match="Reintentos agotados"):
        c.get("https://x")


def test_http_invalid_json_is_treated_as_block():
    c = _client([Resp(200, None, text="<html>captcha</html>")])
    with pytest.raises(BlockedError):
        c.get("https://x", expect="json")


def test_telegram_pauses_on_long_429_and_skips_the_rest():
    s = FakeSession([Resp(429, {"parameters": {"retry_after": 900}})])
    tg = TelegramNotifier("T", "C", min_interval_s=0, session=s)
    assert not tg.send_offer("cap", photo_url="https://img", photo_loader=lambda: b"\xff\xd8")
    assert not tg.send_message("otro mensaje")
    assert len(s.llamadas) == 1 and not tg.available and "900" in tg.last_error


def test_telegram_wait_budget_per_run():
    s = FakeSession([Resp(429, {"parameters": {"retry_after": 25}})] * 4)
    tg = TelegramNotifier("T", "C", min_interval_s=0, session=s, wait_budget_s=60)
    assert not tg.send_message("x")
    assert len(s.llamadas) == 3 and tg.paused                # 25 + 25 caben; el 3er 429 supera los 60 s


def test_telegram_5xx_sets_last_error():
    s = FakeSession([Resp(502, {"description": "Bad Gateway"})] * 4)
    tg = TelegramNotifier("T", "C", min_interval_s=0, session=s)
    assert not tg.send_message("x") and "502" in tg.last_error


def test_healthcheck_ping(monkeypatch):
    from tracker.core import healthcheck

    llamadas = []

    class R:
        ok = True
        status_code = 200

    monkeypatch.setattr(healthcheck.requests, "post", lambda url, data, timeout: llamadas.append(url) or R())
    assert healthcheck.ping("https://hc-ping.com/uuid/", ok=True, body="resumen")
    assert healthcheck.ping("https://hc-ping.com/uuid", ok=False)
    assert not healthcheck.ping("", ok=True)
    assert llamadas == ["https://hc-ping.com/uuid", "https://hc-ping.com/uuid/fail"]

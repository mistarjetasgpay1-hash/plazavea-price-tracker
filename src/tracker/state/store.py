"""Historial de ofertas notificadas con TTL por producto y regla de bajada de precio.

Formato del archivo (JSON):

    {
      "version": 2,
      "updated_at": "2026-10-24T15:00:00+00:00",
      "meta": {"failure_streak": 0, "cursors": {...}},
      "entries": {
        "plazavea:45574": {"price": 1.9, "discount": 67.8,
                           "first_seen": "...", "notified_at": "..."}
      }
    }

Reglas:
  * Producto nuevo                         -> se notifica ("new").
  * Precio <= último avisado × (1 - drop%) -> se vuelve a notificar ("drop").
  * Entradas con aviso más antiguo que el TTL se purgan al cargar, por lo que
    si la oferta sigue vigente se notifica otra vez como nueva.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


@contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    """Candado de archivo (`<historial>.lock`) para que dos instancias del mismo bot no corran
    a la vez (p. ej. PM2 + una prueba manual). Devuelve False si otra instancia lo tiene.
    El sistema operativo lo libera solo si el proceso muere."""
    try:
        import fcntl
    except ImportError:  # Windows: sin candado
        yield True
        return
    ruta = Path(str(path) + ".lock")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(texto: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Entry:
    price: float | None
    discount: float
    first_seen: str
    notified_at: str


class StateStore:
    def __init__(
        self,
        path: Path,
        *,
        ttl_days: float,
        drop_pct: float,
        store_id: str,
        legacy_path: Path | None = None,
        legacy_paths: list[Path] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.path = Path(path)
        self.ttl = timedelta(days=ttl_days)
        self.drop_ratio = max(0.0, drop_pct) / 100.0
        self.store_id = store_id
        fuentes = [legacy_path, *(legacy_paths or [])]
        self.legacy_paths = [Path(p) for p in fuentes if p]
        self.clock = clock
        self.entries: dict[str, Entry] = {}
        self.meta: dict[str, Any] = {}
        self.dirty = False
        self.purged = 0
        self.imported = 0

    # ------------------------------------------------------------------ carga
    def load(self) -> None:
        if self.path.exists():
            self._load_file()
        else:
            for fuente in self.legacy_paths:
                if fuente.exists() and fuente.resolve() != self.path.resolve():
                    self._import_legacy(fuente)
        self.purged = self.purge_expired()

    def _load_file(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data.get("entries", {})
            self.meta = dict(data.get("meta") or {})
            for key, raw in entries.items():
                self.entries[key] = Entry(
                    price=raw.get("price"),
                    discount=float(raw.get("discount") or 0.0),
                    first_seen=str(raw.get("first_seen") or raw.get("notified_at") or ""),
                    notified_at=str(raw.get("notified_at") or ""),
                )
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            respaldo = self.path.with_suffix(self.path.suffix + ".corrupt")
            log.error("Historial ilegible (%s). Se respalda en %s y se empieza de cero.", exc, respaldo)
            try:
                self.path.replace(respaldo)
            except OSError:
                pass
            self.entries, self.meta = {}, {}
            self.dirty = True

    def _import_legacy(self, fuente: Path) -> None:
        """Migra un historial previo sin perder lo ya notificado. Formatos aceptados:
          * v1 (notificados*.json): lista de 'id|url' o 'id|precio'
          * v2 (historial de este bot o de la rama `state`): {"entries": {...}}
        Si un producto aparece en varias fuentes, se queda el aviso más reciente."""
        try:
            datos = json.loads(fuente.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("No se pudo leer el historial antiguo %s: %s", fuente, exc)
            return
        ahora = _iso(self.clock())
        nuevas: dict[str, Entry] = {}
        if isinstance(datos, list):
            for item in datos:
                ident, _, resto = str(item).partition("|")
                if not ident:
                    continue
                try:
                    precio: float | None = float(resto)
                except ValueError:
                    precio = None
                nuevas[f"{self.store_id}:{ident}"] = Entry(precio, 0.0, ahora, ahora)
        elif isinstance(datos, dict) and isinstance(datos.get("entries"), dict):
            for key, raw in datos["entries"].items():
                if not isinstance(raw, dict):
                    continue
                nuevas[key] = Entry(raw.get("price"), float(raw.get("discount") or 0.0),
                                    str(raw.get("first_seen") or raw.get("notified_at") or ahora),
                                    str(raw.get("notified_at") or ahora))
        for key, entry in nuevas.items():
            actual = self.entries.get(key)
            if actual is None or (_parse_iso(entry.notified_at) or datetime.min.replace(tzinfo=timezone.utc)) > \
                    (_parse_iso(actual.notified_at) or datetime.min.replace(tzinfo=timezone.utc)):
                self.entries[key] = entry
        if nuevas:
            self.imported += len(nuevas)
            self.dirty = True
            log.info("Migradas %s entradas del historial previo (%s).", len(nuevas), fuente.name)

    # ------------------------------------------------------------------ reglas
    def purge_expired(self) -> int:
        limite = self.clock() - self.ttl
        vencidas = [
            key for key, entry in self.entries.items()
            if (_parse_iso(entry.notified_at) or limite - timedelta(seconds=1)) < limite
        ]
        for key in vencidas:
            del self.entries[key]
        if vencidas:
            self.dirty = True
        return len(vencidas)

    def check(self, key: str, price: float) -> str | None:
        """'new', 'drop' o None si no corresponde notificar."""
        entry = self.entries.get(key)
        if entry is None:
            return "new"
        if entry.price and price <= entry.price * (1.0 - self.drop_ratio) + 1e-9:
            return "drop"
        return None

    def previous_price(self, key: str) -> float | None:
        entry = self.entries.get(key)
        return entry.price if entry else None

    def mark_notified(self, key: str, price: float, discount: float) -> None:
        ahora = _iso(self.clock())
        anterior = self.entries.get(key)
        self.entries[key] = Entry(
            price=round(price, 2),
            discount=round(discount, 2),
            first_seen=anterior.first_seen if anterior else ahora,
            notified_at=ahora,
        )
        self.dirty = True

    def set_meta(self, key: str, value: Any) -> None:
        if self.meta.get(key) != value:
            self.meta[key] = value
            self.dirty = True

    # ---------------------------------------------------------------- guardado
    def save(self) -> bool:
        if not self.dirty and self.path.exists():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": _iso(self.clock()),
            "meta": self.meta,
            "entries": {k: asdict(v) for k, v in sorted(self.entries.items())},
        }
        temporal = self.path.with_suffix(self.path.suffix + ".tmp")
        temporal.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporal, self.path)  # escritura atómica
        self.dirty = False
        return True

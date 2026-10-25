"""Carga y validación de la configuración YAML + secretos desde variables de entorno.

Un archivo puede heredar de otro con `extends: config.yaml`: los diccionarios se
combinan en profundidad y las listas/valores del archivo hijo reemplazan a los del padre.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Configuración inválida o secretos faltantes."""


def first_env(names: str | list[str]) -> str:
    """Primer valor no vacío entre una o varias variables de entorno."""
    for nombre in [names] if isinstance(names, str) else list(names or []):
        valor = os.getenv(str(nombre), "").strip()
        if valor:
            return valor
    return ""


def env_source(names: str | list[str]) -> str:
    """Nombre de la primera variable de entorno con valor (para logs: nunca expone el valor)."""
    for nombre in [names] if isinstance(names, str) else list(names or []):
        if os.getenv(str(nombre), "").strip():
            return str(nombre)
    return ""


def _names(names: str | list[str]) -> str:
    return names if isinstance(names, str) else " o ".join(map(str, names))


@dataclass
class TelegramConfig:
    # Nombre(s) de la variable de entorno; con una lista se usa la primera que tenga valor.
    token_env: str | list[str] = "TELEGRAM_TOKEN"
    chat_id_env: str | list[str] = "CHAT_ID"
    min_interval_s: float = 3.2        # ~19 msg/min: bajo el límite de 20/min por grupo
    max_individual_alerts: int = 15    # alertas con foto por corrida
    digest_max_items: int = 25         # el excedente va en resúmenes
    send_photos: bool = True
    health_alert_after_failures: int = 3
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Resueltos desde el entorno (no van en el YAML):
    token: str = ""
    chat_id: str = ""

    def group_emoji(self, group: str) -> str:
        return str(self.groups.get(group, {}).get("emoji") or "🔥")

    def group_thread(self, group: str) -> int | None:
        valor = self.groups.get(group, {}).get("thread_id")
        return int(valor) if valor else None


@dataclass
class OffersConfig:
    min_discount_pct: float = 50.0
    min_price: float = 1.0
    exclude_keywords: list[str] = field(default_factory=list)   # palabras en el nombre del producto
    exclude_tags: list[str] = field(default_factory=list)       # regex sobre categoría/colecciones de la tienda


@dataclass
class StateConfig:
    path: str = "state/state.json"
    ttl_days: float = 3.0
    renotify_drop_pct: float = 3.0
    legacy_path: str | None = None
    legacy_paths: list[str] = field(default_factory=list)   # varias fuentes a migrar (v1 y v2)


@dataclass
class HttpConfig:
    timeout_s: float = 20.0
    retries: int = 3
    backoff_base_s: float = 2.0
    delay_min_s: float = 1.0
    delay_max_s: float = 2.5


@dataclass
class HealthcheckConfig:
    """Ping opcional a un monitor externo (p. ej. healthchecks.io) al terminar cada corrida.
    Si el VPS se cae, PM2 se detiene o Telegram rechaza los envíos, el monitor deja de recibir
    pings (o recibe /fail) y te avisa por correo o Telegram."""

    url_env: str | list[str] = ""     # variable(s) de entorno con la URL de ping
    timeout_s: float = 10.0
    url: str = ""                      # resuelta desde el entorno (no va en el YAML)


@dataclass
class AppConfig:
    store_id: str
    store_name: str
    hashtag_prefix: str
    telegram: TelegramConfig
    offers: OffersConfig
    state: StateConfig
    http: HttpConfig
    scraper: dict[str, Any]
    base_dir: Path = Path(".")
    label: str = ""            # nombre del bot (ej. "Alimentos"), para logs y avisos
    healthcheck: HealthcheckConfig = field(default_factory=HealthcheckConfig)

    @property
    def display_name(self) -> str:
        return f"{self.store_name} · {self.label}" if self.label else self.store_name

    def resolve(self, relative: str | None) -> Path | None:
        if not relative:
            return None
        ruta = Path(relative)
        return ruta if ruta.is_absolute() else self.base_dir / ruta


def _build(cls: type, data: dict[str, Any] | None, section: str):
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"La sección '{section}' debe ser un diccionario.")
    permitidos = {f.name for f in fields(cls)} - {"token", "chat_id", "url"}
    desconocidos = set(data) - permitidos
    if desconocidos:
        raise ConfigError(f"Claves desconocidas en '{section}': {', '.join(sorted(desconocidos))}")
    return cls(**data)


def deep_merge(base: dict, override: dict) -> dict:
    resultado = dict(base)
    for clave, valor in override.items():
        if isinstance(valor, dict) and isinstance(resultado.get(clave), dict):
            resultado[clave] = deep_merge(resultado[clave], valor)
        else:
            resultado[clave] = valor
    return resultado


def read_yaml(ruta: Path, _cadena: tuple[Path, ...] = ()) -> dict[str, Any]:
    ruta = ruta.resolve()
    if ruta in _cadena:
        raise ConfigError(f"Herencia circular en 'extends': {' → '.join(p.name for p in (*_cadena, ruta))}")
    if not ruta.exists():
        raise ConfigError(f"No existe el archivo de configuración: {ruta}")
    try:
        raw = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML inválido en {ruta}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{ruta.name} debe contener un diccionario YAML.")
    padre = raw.pop("extends", None)
    if padre:
        raw = deep_merge(read_yaml(ruta.parent / str(padre), (*_cadena, ruta)), raw)
    return raw


def load_config(path: str | Path, *, require_secrets: bool = True) -> AppConfig:
    ruta = Path(path)
    raw = read_yaml(ruta)

    store = raw.get("store") or {}
    if not store.get("id"):
        raise ConfigError("Falta 'store.id' en la configuración.")
    if not isinstance(raw.get("scraper"), dict):
        raise ConfigError("Falta la sección 'scraper'.")

    cfg = AppConfig(
        store_id=str(store["id"]),
        store_name=str(store.get("name") or store["id"]),
        hashtag_prefix=str(store.get("hashtag_prefix") or store.get("name") or store["id"]),
        telegram=_build(TelegramConfig, raw.get("telegram"), "telegram"),
        offers=_build(OffersConfig, raw.get("offers"), "offers"),
        state=_build(StateConfig, raw.get("state"), "state"),
        http=_build(HttpConfig, raw.get("http"), "http"),
        scraper=raw["scraper"],
        base_dir=ruta.resolve().parent,
        label=str(store.get("label") or ""),
        healthcheck=_build(HealthcheckConfig, raw.get("healthcheck"), "healthcheck"),
    )

    for patron in cfg.offers.exclude_tags:
        try:
            re.compile(patron)
        except re.error as exc:
            raise ConfigError(f"offers.exclude_tags: patrón inválido {patron!r}: {exc}") from exc
    if cfg.http.delay_min_s > cfg.http.delay_max_s:
        raise ConfigError("http.delay_min_s no puede ser mayor que http.delay_max_s.")
    if cfg.telegram.max_individual_alerts < 0 or cfg.telegram.digest_max_items < 0:
        raise ConfigError("Los límites de alertas no pueden ser negativos.")

    # Valida categorías, claves y tipos del scraper aquí (y no al crear el scraper): así un error
    # al editar el YAML en caliente hace que el modo continuo siga con la última config válida.
    from ..scrapers import validate_scraper_config

    validate_scraper_config(cfg.scraper)

    cfg.telegram.token = first_env(cfg.telegram.token_env)
    cfg.telegram.chat_id = first_env(cfg.telegram.chat_id_env)
    cfg.healthcheck.url = first_env(cfg.healthcheck.url_env) if cfg.healthcheck.url_env else ""
    if require_secrets and not (cfg.telegram.token and cfg.telegram.chat_id):
        raise ConfigError(
            f"Faltan los secretos {_names(cfg.telegram.token_env)} y/o {_names(cfg.telegram.chat_id_env)} "
            "(en GitHub: Settings → Secrets; en el VPS: archivo .env)."
        )
    return cfg

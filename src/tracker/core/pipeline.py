"""Orquestador: scraper → filtros → historial → Telegram → resumen.

Cada corrida va en dos fases, para que las mejores ofertas salgan cuanto antes (sin peticiones
extra a la tienda):
  1. Página 1 de todas las categorías (las de mayor descuento) → alertas con foto de inmediato.
  2. Resto del paginado → las fotos que queden del cupo y los resúmenes por grupo.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from ..scrapers.base import BaseScraper
from ..state.store import StateStore, exclusive_lock
from ..telegram import formatter
from ..telegram.client import TelegramNotifier
from . import healthcheck
from .config import AppConfig
from .filters import is_official, rejection_reason
from .models import Offer

log = logging.getLogger(__name__)


_PAGO = {"online": "cualquier medio", "tarjeta": "solo tarjeta", "promo": "combo/promo"}


def annotate(level: str, message: str) -> None:
    """Anotación visible en GitHub Actions; en un servidor queda solo en el log."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}")
    (log.error if level == "error" else log.warning)(message)


@dataclass
class RunReport:
    seen: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    candidates: int = 0
    sent_individual: int = 0
    sent_digest: int = 0
    digest_messages: int = 0
    deferred: int = 0
    failed_sends: int = 0
    sent: list[tuple[Offer, str]] = field(default_factory=list)
    failed_keys: set[str] = field(default_factory=set)
    first_alert_s: float | None = None     # segundos desde el inicio hasta la primera alerta

    def reject(self, motivo: str) -> None:
        self.rejected[motivo] = self.rejected.get(motivo, 0) + 1


def _disponible(notifier) -> bool:
    """False si Telegram pidió una pausa larga (429): lo que falta queda para la próxima corrida."""
    return getattr(notifier, "available", True)


def collect_candidates(
    scraper: BaseScraper,
    cfg: AppConfig,
    state: StateStore,
    report: RunReport,
    offers=None,
    vistos: set[str] | None = None,
) -> list[tuple[Offer, str]]:
    """Recorre las ofertas (por defecto, todo el scraper) y devuelve (oferta, motivo) que deben
    notificarse, sin duplicados. `vistos` se comparte entre fases para no repetir productos."""
    vistos = set() if vistos is None else vistos
    candidatos: list[tuple[Offer, str]] = []
    for offer in scraper.iter_offers() if offers is None else offers:
        report.seen += 1
        if offer.key in vistos:
            report.reject("duplicado")
            continue
        vistos.add(offer.key)
        if not is_official(offer, scraper.allowed_seller_ids):
            report.reject("vendedor_no_oficial")
            continue
        motivo = rejection_reason(offer, cfg.offers)
        if motivo:
            report.reject(motivo)
            continue
        razon = state.check(offer.key, offer.effective_price, offer.best_price)
        if razon is None:
            report.reject("ya_notificado")
            continue
        candidatos.append((offer, razon))
    candidatos.sort(key=lambda par: par[0].headline_discount, reverse=True)
    report.candidates += len(candidatos)
    return candidatos


def diversify(candidatos: list[tuple[Offer, str]], n: int) -> list[tuple[Offer, str]]:
    """Elige `n` ofertas alternando grupos (round-robin), para que las alertas con foto no
    queden todas en una sola categoría. Dentro de cada grupo se respeta el mayor descuento."""
    por_grupo: dict[str, list[tuple[Offer, str]]] = {}
    for par in candidatos:              # `candidatos` ya viene ordenado por descuento
        por_grupo.setdefault(par[0].group, []).append(par)
    colas = list(por_grupo.values())
    elegidas: list[tuple[Offer, str]] = []
    while len(elegidas) < n and colas:
        for cola in list(colas):
            if len(elegidas) >= n:
                break
            elegidas.append(cola.pop(0))
            if not cola:
                colas.remove(cola)
    return elegidas


def notify(
    candidatos: list[tuple[Offer, str]],
    *,
    cfg: AppConfig,
    state: StateStore,
    notifier: TelegramNotifier,
    scraper: BaseScraper,
    report: RunReport,
    max_alerts: int | None = None,
    digests: bool = True,
    started_at: float | None = None,
) -> None:
    """Envía hasta `max_alerts` alertas con foto (alternando grupos) y, si `digests`, el resto
    en resúmenes por grupo. Solo se marca en el historial lo que Telegram aceptó."""
    tg = cfg.telegram
    limite = tg.max_individual_alerts if max_alerts is None else max_alerts
    individuales = diversify(candidatos, max(0, limite))
    elegidas = {id(par) for par in individuales}
    resto = [par for par in candidatos if id(par) not in elegidas][: tg.digest_max_items] if digests else []
    antes_individual, antes_digest = report.sent_individual, report.sent_digest

    for offer, razon in individuales:
        if not _disponible(notifier):
            break
        anterior, via_tarjeta = (state.drop_reference(offer.key, offer.effective_price, offer.best_price)
                                 if razon == "drop" else (None, False))
        caption = formatter.offer_caption(
            offer,
            reason=razon,
            previous_price=anterior,
            store_name=cfg.store_name,
            hashtag_prefix=cfg.hashtag_prefix,
            emoji=tg.group_emoji(offer.group),
            drop_via_card=via_tarjeta,
        )
        ok = notifier.send_offer(
            caption,
            photo_url=offer.image_url,
            thread_id=tg.group_thread(offer.group),
            photo_loader=lambda o=offer: scraper.fetch_image(o),
        )
        if ok:
            state.mark_notified(offer.key, offer.effective_price, offer.headline_discount, offer.best_price)
            report.sent_individual += 1
            report.sent.append((offer, razon))
            if report.first_alert_s is None and started_at is not None:
                report.first_alert_s = time.monotonic() - started_at
            log.info("[OK] -%.1f%% %s (%s, %s)", offer.headline_discount, offer.name[:60], razon, offer.qualified_by)
        elif _disponible(notifier):
            report.failed_sends += 1
            report.failed_keys.add(offer.key)

    # Resúmenes: uno o más mensajes por grupo (cada uno ≤ 4096 caracteres), al hilo del grupo.
    por_grupo: dict[str, list[tuple[Offer, str]]] = {}
    for par in resto:
        por_grupo.setdefault(par[0].group, []).append(par)
    for grupo, pendientes in por_grupo.items():
        while pendientes and _disponible(notifier):
            texto, incluidas = formatter.digest_message(
                pendientes, store_name=cfg.store_name, hashtag_prefix=cfg.hashtag_prefix,
                group=grupo, emoji=tg.group_emoji(grupo),
            )
            if not incluidas:
                break
            if not notifier.send_message(texto, thread_id=tg.group_thread(grupo), preview=False):
                if _disponible(notifier):
                    report.failed_sends += 1
                break
            for offer, razon in incluidas:
                state.mark_notified(offer.key, offer.effective_price, offer.headline_discount, offer.best_price)
                report.sent.append((offer, razon))
            report.sent_digest += len(incluidas)
            report.digest_messages += 1
            pendientes = pendientes[len(incluidas):]

    enviadas = (report.sent_individual - antes_individual) + (report.sent_digest - antes_digest)
    report.deferred = max(0, len(candidatos) - enviadas)


def update_health(
    scraper: BaseScraper, cfg: AppConfig, state: StateStore, notifier: TelegramNotifier
) -> None:
    """Avisa si el bot lleva N corridas sin datos y, si no, si alguna categoría lleva N corridas
    sin productos (p. ej. Plaza Vea renombró su ruta), y otra vez al recuperarse. Cada aviso se
    marca como dado solo si Telegram lo aceptó: si se pierde (Telegram en pausa), se reintenta."""
    racha_previa = int(state.meta.get("failure_streak", 0))
    umbral = cfg.telegram.health_alert_after_failures
    # Historiales previos sin la marca: si la racha ya superaba el umbral, el aviso ya se dio.
    avisado = bool(state.meta.get("failure_alerted", bool(umbral) and racha_previa >= umbral))
    stats = scraper.stats
    if stats.total_failure:
        racha = racha_previa + 1
        state.set_meta("failure_streak", racha)
        annotate("warning", f"{cfg.display_name}: corrida sin datos ({racha} seguidas). {stats.errors[:2]}")
        if umbral and racha >= umbral and not avisado:
            avisado = notifier.send_message(
                formatter.health_message(cfg.display_name, racha, stats.errors), silent=True, preview=False
            )
        state.set_meta("failure_alerted", avisado)
        return          # el aviso general ya cubre a todas las categorías
    if avisado and notifier.send_message(formatter.recovery_message(cfg.display_name), silent=True, preview=False):
        avisado = False
    state.set_meta("failure_streak", 0)
    state.set_meta("failure_alerted", avisado)
    _update_category_health(scraper, cfg, state, notifier, umbral)


def _update_category_health(
    scraper: BaseScraper, cfg: AppConfig, state: StateStore, notifier: TelegramNotifier, umbral: int
) -> None:
    previas = {str(k): int(v) for k, v in (state.meta.get("category_streaks") or {}).items()}
    avisadas = set(state.meta.get("category_alerted") or [])
    sin_datos = list(dict.fromkeys(scraper.stats.categories_without_data))
    con_datos = list(dict.fromkeys(scraper.stats.ok_categories))
    rachas = {key: previas.get(key, 0) + 1 for key in sin_datos}
    caidas = [k for k in sin_datos if umbral and rachas[k] >= umbral and k not in avisadas]
    recuperadas = [k for k in con_datos if k in avisadas]
    if caidas:
        rutas = {c.key: c.path for c in getattr(scraper, "categories", [])}
        annotate("warning", f"{cfg.display_name}: sin productos {umbral}+ corridas seguidas en: {', '.join(caidas)}")
        racha = min(rachas[k] for k in caidas)
        if notifier.send_message(formatter.category_health_message(cfg.display_name, caidas, racha, rutas),
                                 silent=True, preview=False):
            avisadas |= set(caidas)
    if recuperadas and notifier.send_message(formatter.category_recovery_message(cfg.display_name, recuperadas),
                                             silent=True, preview=False):
        avisadas -= set(recuperadas)
    state.set_meta("category_streaks", rachas)
    # Solo se recuerdan las categorías que siguen en la configuración.
    state.set_meta("category_alerted", sorted(avisadas & set(sin_datos + con_datos)))


def summary_line(cfg: AppConfig, report: RunReport, scraper: BaseScraper, duracion: float) -> str:
    primera = f" | 1ª alerta a los {report.first_alert_s:.0f}s" if report.first_alert_s is not None else ""
    return (
        f"Resumen {cfg.display_name} | vistos={report.seen} candidatos={report.candidates} "
        f"enviados={report.sent_individual}+{report.sent_digest}(resumen) pendientes={report.deferred} "
        f"fallos_envio={report.failed_sends} | páginas ok={scraper.stats.pages_ok} "
        f"fallidas={scraper.stats.pages_failed} modo={scraper.stats.mode} | {duracion:.1f}s{primera}"
    )


def write_summary(cfg: AppConfig, report: RunReport, scraper: BaseScraper, duracion: float) -> None:
    rechazos = ", ".join(f"{k}: {v}" for k, v in sorted(report.rejected.items())) or "—"
    log.info("%s", summary_line(cfg, report, scraper, duracion))
    log.info("Descartes: %s", rechazos)
    if scraper.stats.parse_errors:
        log.warning("Productos ignorados por datos inesperados: %s", scraper.stats.parse_errors)

    destino = os.getenv("GITHUB_STEP_SUMMARY")
    if not destino:
        return
    filas = "\n".join(
        f"| -{formatter.pct(o.headline_discount)}% | {o.name[:60].replace('|', '/')} | S/ {o.effective_price:,.2f} | "
        f"{_PAGO.get(o.qualified_by, o.qualified_by)} | {o.group} | {r} |"
        for o, r in report.sent[:40]
    )
    primera = f"{report.first_alert_s:.0f}s" if report.first_alert_s is not None else "—"
    md = (
        f"## {cfg.display_name}\n\n"
        f"| Métrica | Valor |\n|---|---|\n"
        f"| Productos vistos | {report.seen} |\n| Candidatos | {report.candidates} |\n"
        f"| Enviados (foto) | {report.sent_individual} |\n"
        f"| Enviados en resúmenes | {report.sent_digest} ({report.digest_messages} mensajes) |\n"
        f"| Pendientes p/ próxima corrida | {report.deferred} |\n| Fallos de envío | {report.failed_sends} |\n"
        f"| Primera alerta (desde el inicio) | {primera} |\n"
        f"| Páginas OK / fallidas | {scraper.stats.pages_ok} / {scraper.stats.pages_failed} |\n"
        f"| Productos ignorados (datos inesperados) | {scraper.stats.parse_errors} |\n"
        f"| Modo de extracción | {scraper.stats.mode} |\n| Duración | {duracion:.1f}s |\n\n"
        f"**Descartes:** {rechazos}\n\n"
    )
    if filas:
        md += "| Dto. | Producto | Precio | Califica por | Grupo | Motivo |\n|---|---|---|---|---|---|\n" + filas + "\n"
    if scraper.stats.errors:
        md += "\n**Errores:**\n" + "\n".join(f"- `{e[:200]}`" for e in scraper.stats.errors[:10]) + "\n"
    with open(destino, "a", encoding="utf-8") as fh:
        fh.write(md)


def run(cfg: AppConfig, *, dry_run: bool = False, max_alerts: int | None = None) -> int:
    """Una corrida completa. En modo real toma un candado sobre el historial: si otra
    instancia del mismo bot está corriendo, esta corrida se omite (código 3)."""
    if dry_run:  # la simulación no escribe historial: puede convivir con el bot en producción
        return _run(cfg, dry_run=True, max_alerts=max_alerts)
    with exclusive_lock(cfg.resolve(cfg.state.path)) as adquirido:
        if not adquirido:
            log.error("Otra instancia de %s está corriendo (candado en %s.lock); se omite esta corrida.",
                      cfg.display_name, cfg.state.path)
            return 3
        return _run(cfg, dry_run=False, max_alerts=max_alerts)


def _por_descuento(candidatos: list[tuple[Offer, str]]) -> list[tuple[Offer, str]]:
    return sorted(candidatos, key=lambda par: par[0].headline_discount, reverse=True)


def run_phases(
    scraper: BaseScraper,
    cfg: AppConfig,
    state: StateStore,
    notifier: TelegramNotifier,
    report: RunReport,
    *,
    max_alerts: int | None = None,
    started_at: float | None = None,
) -> None:
    """Las dos fases de una corrida: primero la página 1 de todas las categorías (fotos de
    inmediato) y luego el resto del paginado (fotos que queden del cupo + resúmenes)."""
    limite = cfg.telegram.max_individual_alerts if max_alerts is None else max_alerts
    vistos: set[str] = set()

    primeras = collect_candidates(scraper, cfg, state, report, offers=scraper.iter_head(), vistos=vistos)
    if primeras:
        notify(primeras, cfg=cfg, state=state, notifier=notifier, scraper=scraper, report=report,
               max_alerts=limite, digests=False, started_at=started_at)
        log.info("Fase 1: %s candidatos en la página 1, %s alertas con foto enviadas.",
                 len(primeras), report.sent_individual)
    enviadas = {o.key for o, _ in report.sent}
    # Las que fallaron en la fase 1 no se reintentan en esta corrida (quedan para la próxima).
    pendientes = [p for p in primeras if p[0].key not in enviadas and p[0].key not in report.failed_keys]

    resto = collect_candidates(scraper, cfg, state, report, offers=scraper.iter_rest(), vistos=vistos)
    notify(_por_descuento(pendientes + resto), cfg=cfg, state=state, notifier=notifier, scraper=scraper,
           report=report, max_alerts=max(0, limite - report.sent_individual), digests=True, started_at=started_at)
    report.deferred = max(0, report.candidates - report.sent_individual - report.sent_digest)
    if not _disponible(notifier):
        annotate("warning", f"Telegram en pausa ({notifier.last_error}): {report.deferred} ofertas quedan "
                            "para la próxima corrida.")


def _run(cfg: AppConfig, *, dry_run: bool, max_alerts: int | None) -> int:
    from ..scrapers import build_scraper

    inicio = time.monotonic()
    state = StateStore(
        cfg.resolve(cfg.state.path),
        ttl_days=cfg.state.ttl_days,
        drop_pct=cfg.state.renotify_drop_pct,
        store_id=cfg.store_id,
        legacy_path=cfg.resolve(cfg.state.legacy_path),
        legacy_paths=[cfg.resolve(p) for p in cfg.state.legacy_paths],
    )
    state.load()
    log.info(
        "Historial: %s entradas activas (%s vencidas purgadas, %s migradas). TTL=%s días, re-aviso si baja ≥%s%%.",
        len(state.entries), state.purged, state.imported, cfg.state.ttl_days, cfg.state.renotify_drop_pct,
    )

    notifier = TelegramNotifier(
        cfg.telegram.token,
        cfg.telegram.chat_id,
        min_interval_s=cfg.telegram.min_interval_s,
        send_photos=cfg.telegram.send_photos,
        dry_run=dry_run,
    )
    scraper = build_scraper(cfg, state.meta.setdefault("scraper", {}))
    report = RunReport()
    codigo = 0
    completada, fallo = False, ""
    try:
        run_phases(scraper, cfg, state, notifier, report, max_alerts=max_alerts, started_at=inicio)
        update_health(scraper, cfg, state, notifier)
        if report.failed_sends and not report.sent:
            # Token/chat inválido o Telegram caído: se marca el job en rojo para que GitHub avise.
            annotate("error", f"No se pudo enviar ninguna alerta a Telegram: {notifier.last_error}")
            codigo = 1
        completada = True
    except Exception as exc:
        fallo = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        scraper.close()
        notifier.close()
        if scraper.meta_changed:
            state.dirty = True
        if dry_run:
            log.info("[DRY-RUN] No se guarda el historial.")
        elif state.save():
            log.info("Historial guardado en %s (%s entradas).", state.path, len(state.entries))
        duracion = time.monotonic() - inicio
        write_summary(cfg, report, scraper, duracion)
        if not dry_run and (completada or fallo):
            sano = completada and codigo == 0 and not scraper.stats.total_failure
            detalle = summary_line(cfg, report, scraper, duracion) + (f"\nError: {fallo}" if fallo else "")
            healthcheck.ping(cfg.healthcheck.url, ok=sano, body=detalle, timeout_s=cfg.healthcheck.timeout_s)
    return codigo

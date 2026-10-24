"""Orquestador: scraper → filtros → historial → Telegram → resumen."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from ..telegram import formatter
from ..scrapers.base import BaseScraper
from .config import AppConfig
from .filters import is_official, rejection_reason
from .models import Offer
from ..state.store import StateStore, exclusive_lock
from ..telegram.client import TelegramNotifier

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

    def reject(self, motivo: str) -> None:
        self.rejected[motivo] = self.rejected.get(motivo, 0) + 1


def collect_candidates(
    scraper: BaseScraper, cfg: AppConfig, state: StateStore, report: RunReport
) -> list[tuple[Offer, str]]:
    """Recorre el scraper y devuelve (oferta, motivo) que deben notificarse, sin duplicados."""
    vistos: set[str] = set()
    candidatos: list[tuple[Offer, str]] = []
    for offer in scraper.iter_offers():
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
        razon = state.check(offer.key, offer.effective_price)
        if razon is None:
            report.reject("ya_notificado")
            continue
        candidatos.append((offer, razon))
    candidatos.sort(key=lambda par: par[0].headline_discount, reverse=True)
    report.candidates = len(candidatos)
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
) -> None:
    tg = cfg.telegram
    limite = tg.max_individual_alerts if max_alerts is None else max_alerts
    individuales = diversify(candidatos, limite)
    elegidas = {id(par) for par in individuales}
    resto = [par for par in candidatos if id(par) not in elegidas][: tg.digest_max_items]

    for offer, razon in individuales:
        caption = formatter.offer_caption(
            offer,
            reason=razon,
            previous_price=state.previous_price(offer.key),
            store_name=cfg.store_name,
            hashtag_prefix=cfg.hashtag_prefix,
            emoji=tg.group_emoji(offer.group),
        )
        ok = notifier.send_offer(
            caption,
            photo_url=offer.image_url,
            thread_id=tg.group_thread(offer.group),
            photo_loader=lambda o=offer: scraper.fetch_image(o),
        )
        if ok:
            state.mark_notified(offer.key, offer.effective_price, offer.headline_discount)
            report.sent_individual += 1
            report.sent.append((offer, razon))
            log.info("[OK] -%.1f%% %s (%s, %s)", offer.headline_discount, offer.name[:60], razon, offer.qualified_by)
        else:
            report.failed_sends += 1

    # Resúmenes: uno o más mensajes por grupo (cada uno ≤ 4096 caracteres), al hilo del grupo.
    por_grupo: dict[str, list[tuple[Offer, str]]] = {}
    for par in resto:
        por_grupo.setdefault(par[0].group, []).append(par)
    for grupo, pendientes in por_grupo.items():
        while pendientes:
            texto, incluidas = formatter.digest_message(
                pendientes, store_name=cfg.store_name, hashtag_prefix=cfg.hashtag_prefix,
                group=grupo, emoji=tg.group_emoji(grupo),
            )
            if not incluidas:
                break
            if not notifier.send_message(texto, thread_id=tg.group_thread(grupo), preview=False):
                report.failed_sends += 1
                break
            for offer, razon in incluidas:
                state.mark_notified(offer.key, offer.effective_price, offer.headline_discount)
                report.sent.append((offer, razon))
            report.sent_digest += len(incluidas)
            report.digest_messages += 1
            pendientes = pendientes[len(incluidas):]

    report.deferred = max(0, len(candidatos) - report.sent_individual - report.sent_digest)


def update_health(
    scraper: BaseScraper, cfg: AppConfig, state: StateStore, notifier: TelegramNotifier
) -> None:
    racha_previa = int(state.meta.get("failure_streak", 0))
    umbral = cfg.telegram.health_alert_after_failures
    if scraper.stats.total_failure:
        racha = racha_previa + 1
        state.set_meta("failure_streak", racha)
        annotate("warning", f"{cfg.display_name}: corrida sin datos ({racha} seguidas). {scraper.stats.errors[:2]}")
        if umbral and racha == umbral:
            notifier.send_message(
                formatter.health_message(cfg.display_name, racha, scraper.stats.errors), silent=True, preview=False
            )
    else:
        if umbral and racha_previa >= umbral:
            notifier.send_message(formatter.recovery_message(cfg.display_name), silent=True, preview=False)
        state.set_meta("failure_streak", 0)


def write_summary(cfg: AppConfig, report: RunReport, scraper: BaseScraper, duracion: float) -> None:
    rechazos = ", ".join(f"{k}: {v}" for k, v in sorted(report.rejected.items())) or "—"
    log.info(
        "Resumen %s | vistos=%s candidatos=%s enviados=%s+%s(resumen) pendientes=%s fallos_envio=%s | "
        "páginas ok=%s fallidas=%s modo=%s | %.1fs",
        cfg.display_name, report.seen, report.candidates, report.sent_individual, report.sent_digest,
        report.deferred, report.failed_sends, scraper.stats.pages_ok, scraper.stats.pages_failed,
        scraper.stats.mode, duracion,
    )
    log.info("Descartes: %s", rechazos)

    destino = os.getenv("GITHUB_STEP_SUMMARY")
    if not destino:
        return
    filas = "\n".join(
        f"| -{o.headline_discount:.0f}% | {o.name[:60].replace('|', '/')} | S/ {o.effective_price:,.2f} | "
        f"{_PAGO.get(o.qualified_by, o.qualified_by)} | {o.group} | {r} |"
        for o, r in report.sent[:40]
    )
    md = (
        f"## {cfg.display_name}\n\n"
        f"| Métrica | Valor |\n|---|---|\n"
        f"| Productos vistos | {report.seen} |\n| Candidatos | {report.candidates} |\n"
        f"| Enviados (foto) | {report.sent_individual} |\n"
        f"| Enviados en resúmenes | {report.sent_digest} ({report.digest_messages} mensajes) |\n"
        f"| Pendientes p/ próxima corrida | {report.deferred} |\n| Fallos de envío | {report.failed_sends} |\n"
        f"| Páginas OK / fallidas | {scraper.stats.pages_ok} / {scraper.stats.pages_failed} |\n"
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
    try:
        candidatos = collect_candidates(scraper, cfg, state, report)
        notify(candidatos, cfg=cfg, state=state, notifier=notifier, scraper=scraper,
               report=report, max_alerts=max_alerts)
        update_health(scraper, cfg, state, notifier)
        if report.failed_sends and not report.sent:
            # Token/chat inválido o Telegram caído: se marca el job en rojo para que GitHub avise.
            annotate("error", f"No se pudo enviar ninguna alerta a Telegram: {notifier.last_error}")
            codigo = 1
    finally:
        scraper.close()
        if scraper.meta_changed:
            state.dirty = True
        if dry_run:
            log.info("[DRY-RUN] No se guarda el historial.")
        elif state.save():
            log.info("Historial guardado en %s (%s entradas).", state.path, len(state.entries))
        write_summary(cfg, report, scraper, time.monotonic() - inicio)
    return codigo

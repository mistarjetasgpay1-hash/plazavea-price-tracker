"""Orquestador: scraper → filtros → historial → Telegram → resumen."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from . import formatter
from .base import BaseScraper
from .config import AppConfig
from .filters import is_official, rejection_reason
from .models import Offer
from .state import StateStore
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    seen: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    candidates: int = 0
    sent_individual: int = 0
    sent_digest: int = 0
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
        razon = state.check(offer.key, offer.price)
        if razon is None:
            report.reject("ya_notificado")
            continue
        candidatos.append((offer, razon))
    candidatos.sort(key=lambda par: par[0].discount, reverse=True)
    report.candidates = len(candidatos)
    return candidatos


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
    individuales = candidatos[:limite]
    resto = candidatos[limite: limite + tg.digest_max_items]

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
            state.mark_notified(offer.key, offer.price, offer.discount)
            report.sent_individual += 1
            report.sent.append((offer, razon))
            log.info("[OK] -%.1f%% %s (%s)", offer.discount, offer.name[:60], razon)
        else:
            report.failed_sends += 1

    if resto:
        texto, incluidas = formatter.digest_message(
            resto, store_name=cfg.store_name, hashtag_prefix=cfg.hashtag_prefix
        )
        # El resumen va al hilo del grupo más frecuente (si se usan Temas).
        grupos = [o.group for o, _ in incluidas]
        hilo = tg.group_thread(max(set(grupos), key=grupos.count)) if grupos else None
        if incluidas and notifier.send_message(texto, thread_id=hilo, preview=False):
            for offer, razon in incluidas:
                state.mark_notified(offer.key, offer.price, offer.discount)
                report.sent.append((offer, razon))
            report.sent_digest = len(incluidas)
        else:
            report.failed_sends += 1

    report.deferred = max(0, len(candidatos) - report.sent_individual - report.sent_digest)


def update_health(
    scraper: BaseScraper, cfg: AppConfig, state: StateStore, notifier: TelegramNotifier
) -> None:
    racha_previa = int(state.meta.get("failure_streak", 0))
    umbral = cfg.telegram.health_alert_after_failures
    if scraper.stats.total_failure:
        racha = racha_previa + 1
        state.set_meta("failure_streak", racha)
        print(f"::warning::{cfg.store_name}: corrida sin datos ({racha} seguidas). {scraper.stats.errors[:2]}")
        if umbral and racha == umbral:
            notifier.send_message(
                formatter.health_message(cfg.store_name, racha, scraper.stats.errors), silent=True, preview=False
            )
    else:
        if umbral and racha_previa >= umbral:
            notifier.send_message(formatter.recovery_message(cfg.store_name), silent=True, preview=False)
        state.set_meta("failure_streak", 0)


def write_summary(cfg: AppConfig, report: RunReport, scraper: BaseScraper, duracion: float) -> None:
    rechazos = ", ".join(f"{k}: {v}" for k, v in sorted(report.rejected.items())) or "—"
    log.info(
        "Resumen %s | vistos=%s candidatos=%s enviados=%s+%s(resumen) pendientes=%s fallos_envio=%s | "
        "páginas ok=%s fallidas=%s modo=%s | %.1fs",
        cfg.store_name, report.seen, report.candidates, report.sent_individual, report.sent_digest,
        report.deferred, report.failed_sends, scraper.stats.pages_ok, scraper.stats.pages_failed,
        scraper.stats.mode, duracion,
    )
    log.info("Descartes: %s", rechazos)

    destino = os.getenv("GITHUB_STEP_SUMMARY")
    if not destino:
        return
    filas = "\n".join(
        f"| -{o.discount:.0f}% | {o.name[:60].replace('|', '/')} | S/ {o.price:,.2f} | {o.group} | {r} |"
        for o, r in report.sent[:40]
    )
    md = (
        f"## {cfg.store_name}\n\n"
        f"| Métrica | Valor |\n|---|---|\n"
        f"| Productos vistos | {report.seen} |\n| Candidatos | {report.candidates} |\n"
        f"| Enviados (foto) | {report.sent_individual} |\n| Enviados (resumen) | {report.sent_digest} |\n"
        f"| Pendientes p/ próxima corrida | {report.deferred} |\n| Fallos de envío | {report.failed_sends} |\n"
        f"| Páginas OK / fallidas | {scraper.stats.pages_ok} / {scraper.stats.pages_failed} |\n"
        f"| Modo de extracción | {scraper.stats.mode} |\n| Duración | {duracion:.1f}s |\n\n"
        f"**Descartes:** {rechazos}\n\n"
    )
    if filas:
        md += "| Dto. | Producto | Precio | Grupo | Motivo |\n|---|---|---|---|---|\n" + filas + "\n"
    if scraper.stats.errors:
        md += "\n**Errores:**\n" + "\n".join(f"- `{e[:200]}`" for e in scraper.stats.errors[:10]) + "\n"
    with open(destino, "a", encoding="utf-8") as fh:
        fh.write(md)


def run(cfg: AppConfig, *, dry_run: bool = False, max_alerts: int | None = None) -> int:
    from .scraper import build_scraper  # módulo propio de cada repositorio

    inicio = time.monotonic()
    state = StateStore(
        cfg.resolve(cfg.state.path),
        ttl_days=cfg.state.ttl_days,
        drop_pct=cfg.state.renotify_drop_pct,
        store_id=cfg.store_id,
        legacy_path=cfg.resolve(cfg.state.legacy_path),
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
            print(f"::error::No se pudo enviar ninguna alerta a Telegram: {notifier.last_error}")
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

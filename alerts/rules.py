"""Declarative alert rules (CLAUDE.md sections 2, 8 phase 3).

Rules are configured in ``settings.yaml`` under ``alerts.rules`` and evaluated against
the data already in the DB (via the transform layer). Each fired condition becomes an
:class:`Alert` with a stable ``dedup_key``; :func:`dispatch_alerts` sends only the ones
not already in ``alerts_log`` (so the same event is not re-sent), then records them.

Alerts fire only on **actionable, pre-written** conditions (§2): the panel is a discipline
tool, not a ticker. Deduping is per condition per day, so a persistent condition produces
at most one message a day.

Rule kinds (evaluators):
    etf_outflow_streak   ETF net outflows for >= N consecutive days.
    funding_crowded      |funding z-score| >= threshold on a tracked asset.
    tvl_drop             A thesis's TVL fell >= X% over 7 days (possible invalidation).
    unlock_soon          A tracked asset unlocks within N days (from assets_meta).
    monthly_dca_reminder Monthly contribution reminder with level 1/2 context.
    macro_release_soon   A high-impact macro release (CPI/PCE/NFP) or FOMC within N days.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from core.config import Settings
from core.logging_setup import get_logger
from db.queries import upcoming_events
from transform.indicators import macro_table
from transform.rally_quality import etf_flow_summary, market_structure_table

log = get_logger(__name__)


@dataclass(frozen=True)
class Alert:
    """One fired alert. ``dedup_key`` is the primary key in ``alerts_log``."""

    rule_id: str
    dedup_key: str
    message: str


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- Evaluators --------------------------------------------------------------


def _etf_outflow_streak(conn: sqlite3.Connection, settings: Settings, params: dict) -> list[Alert]:
    min_days = int(params.get("min_days", 3))
    out: list[Alert] = []
    for r in etf_flow_summary(conn, settings).to_dict("records"):
        if r["streak_sign"] < 0 and r["streak_days"] >= min_days:
            out.append(
                Alert(
                    "etf_outflow_streak",
                    f"etf_outflow_streak:{r['asset']}:{_today()}",
                    f"🔴 *Flujos ETF* — {r['asset']} lleva *{r['streak_days']} días* de salidas "
                    f"netas ({r['sum_5d']:+,.0f} M en 5d). Demanda institucional débil; el rally "
                    f"pierde sustento (§2 nivel 2).",
                )
            )
    return out


def _funding_crowded(conn: sqlite3.Connection, settings: Settings, params: dict) -> list[Alert]:
    threshold = float(params.get("z_threshold", 2.0))
    out: list[Alert] = []
    for r in market_structure_table(conn, settings).to_dict("records"):
        z = r["funding_z"]
        if z is None:
            continue
        if abs(z) >= threshold:
            side = "largos hacinados (riesgo de cascada bajista)" if z > 0 else (
                "cortos hacinados (condiciones de short squeeze)"
            )
            out.append(
                Alert(
                    "funding_crowded",
                    f"funding_crowded:{r['symbol']}:{_today()}",
                    f"⚠️ *Funding* — {r['symbol']} z-score {z:+.2f} (|z|≥{threshold:g}): {side} "
                    f"(§2 nivel 2, §8).",
                )
            )
    return out


def _tvl_drop(conn: sqlite3.Connection, settings: Settings, params: dict) -> list[Alert]:
    pct = float(params.get("pct", 20.0))
    out: list[Alert] = []
    from transform.indicators import thesis_tvl_table

    for r in thesis_tvl_table(conn, settings).to_dict("records"):
        chg = r["tvl_chg_7d"]
        if chg is not None and chg <= -pct:
            out.append(
                Alert(
                    "tvl_drop",
                    f"tvl_drop:{r['symbol']}:{_today()}",
                    f"🔻 *TVL* — {r['symbol']} ({r['thesis_category']}) cae *{chg:.1f}%* en 7d "
                    f"(≥{pct:g}%). Posible invalidación de tesis (§3, §5) — revisar la posición.",
                )
            )
    return out


def _unlock_soon(conn: sqlite3.Connection, settings: Settings, params: dict) -> list[Alert]:
    within = int(params.get("within_days", 7))
    today = datetime.now(timezone.utc).date()
    out: list[Alert] = []
    for symbol, meta in settings.asset_meta.items():
        raw = meta.get("next_unlock") if isinstance(meta, dict) else None
        if not raw:
            continue
        try:
            date = datetime.strptime(str(raw), "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (date - today).days
        if 0 <= days <= within:
            out.append(
                Alert(
                    "unlock_soon",
                    f"unlock_soon:{symbol}:{raw}",
                    f"🔓 *Unlock* — {symbol} desbloquea tokens el *{raw}* (en {days} d). "
                    f"Presión vendedora potencial; considerar posponer el tramo (§3 nivel 3).",
                )
            )
    return out


def _monthly_dca_reminder(conn: sqlite3.Connection, settings: Settings, params: dict) -> list[Alert]:
    day_of_month = int(params.get("day_of_month", 1))
    now = datetime.now(timezone.utc)
    if now.day < day_of_month:
        return []
    minimum = settings.raw.get("dca", {}).get("monthly_contribution_min_usd", 200)

    # Level 1/2 context: crowded-funding count + mechanical rallies + BTC dominance.
    ms = market_structure_table(conn, settings)
    crowded = int((ms["funding_z"].abs() >= 2).sum()) if not ms.empty else 0
    mechanical = int((ms["rally_state"] == "mecanico").sum()) if not ms.empty else 0
    macro_ok = int(macro_table(conn, settings)["value"].notna().sum()) > 0

    message = (
        f"🗓️ *Aporte mensual* (mín. ${minimum}). Antes de ejecutar el tramo, revisa niveles 1-2:\n"
        f"• Macro: {'datos disponibles' if macro_ok else 'sin datos (configura FRED)'}.\n"
        f"• Estructura: {crowded} activo(s) con funding extremo, {mechanical} rally(s) mecánico(s).\n"
        f"Si 1-2 están en rojo o hay un release/unlock inminente, considera posponer unos días (§2)."
    )
    return [Alert("monthly_dca_reminder", f"monthly_dca_reminder:{now:%Y-%m}", message)]


def _macro_release_soon(conn: sqlite3.Connection, settings: Settings, params: dict) -> list[Alert]:
    """High-impact macro release (CPI/PCE/NFP) or FOMC within N days -> 'no DCA today'."""
    within = int(params.get("within_days", 3))
    out: list[Alert] = []
    for e in upcoming_events(conn, within_days=within, categories=("macro",)):
        when = "hoy" if e["days_until"] == 0 else f"en {e['days_until']} d"
        out.append(
            Alert(
                "macro_release_soon",
                f"macro_release_soon:{e['label']}:{e['date']}",
                f"📅 *Release macro* — {e['label']} el *{e['date']}* ({when}). Evento de alto "
                f"impacto: no ejecutes el tramo de DCA todavía; espera al dato (niveles 1 y 4).",
            )
        )
    today = datetime.now(timezone.utc).date()
    for raw in settings.raw.get("macro_calendar", {}).get("fomc_dates", []) or []:
        try:
            date = datetime.strptime(str(raw), "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (date - today).days
        if 0 <= days <= within:
            when = "hoy" if days == 0 else f"en {days} d"
            out.append(
                Alert(
                    "macro_release_soon",
                    f"macro_release_soon:FOMC:{raw}",
                    f"🏛️ *FOMC* — decisión de tipos el *{raw}* ({when}). No ejecutes el tramo de "
                    f"DCA hasta después del anuncio (niveles 1 y 4).",
                )
            )
    return out


_EVALUATORS: dict[str, Callable[[sqlite3.Connection, Settings, dict], list[Alert]]] = {
    "etf_outflow_streak": _etf_outflow_streak,
    "funding_crowded": _funding_crowded,
    "tvl_drop": _tvl_drop,
    "unlock_soon": _unlock_soon,
    "monthly_dca_reminder": _monthly_dca_reminder,
    "macro_release_soon": _macro_release_soon,
}


def evaluate_rules(conn: sqlite3.Connection, settings: Settings) -> list[Alert]:
    """Evaluate all configured rules and return every fired Alert (pre-dedup)."""
    alerts: list[Alert] = []
    for rule in settings.raw.get("alerts", {}).get("rules", []):
        kind = rule.get("kind")
        evaluator = _EVALUATORS.get(kind)
        if evaluator is None:
            log.warning("Unknown alert rule kind '%s'; skipping.", kind)
            continue
        try:
            alerts.extend(evaluator(conn, settings, rule))
        except Exception:  # noqa: BLE001 — one bad rule must not sink the rest
            log.exception("Alert rule '%s' failed.", kind)
    return alerts


def _already_sent(conn: sqlite3.Connection, dedup_key: str) -> bool:
    """True only if this alert was already **delivered**.

    A prior dry-run / failed send is logged with ``delivered=False`` and must be retried
    (e.g. after Telegram is configured), so it does not count as sent.
    """
    row = conn.execute(
        "SELECT payload FROM alerts_log WHERE alert_id = ?", (dedup_key,)
    ).fetchone()
    return bool(row) and "delivered=True" in (row[0] or "")


def dispatch_alerts(conn: sqlite3.Connection, settings: Settings, sender: Any) -> tuple[int, int]:
    """Evaluate rules, send alerts not already logged, and record them.

    Args:
        sender: object with ``send(message) -> bool`` (e.g. TelegramSender).

    Returns:
        (fired, sent) — total conditions that fired and how many were newly sent.
    """
    fired = evaluate_rules(conn, settings)
    sent = 0
    now = datetime.now(timezone.utc).isoformat()
    for alert in fired:
        if _already_sent(conn, alert.dedup_key):
            continue
        delivered = sender.send(alert.message)
        with conn:
            # Upsert so a prior delivered=False row is upgraded when a retry succeeds.
            conn.execute(
                "INSERT INTO alerts_log (alert_id, rule_id, fired_at, payload) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(alert_id) DO UPDATE SET "
                "fired_at = excluded.fired_at, payload = excluded.payload",
                (alert.dedup_key, alert.rule_id, now, f"delivered={delivered}"),
            )
        if delivered:
            sent += 1
    log.info("Alerts: %d fired, %d newly sent.", len(fired), sent)
    return len(fired), sent

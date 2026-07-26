"""Streamlit dashboard (CLAUDE.md sections 2, 8 phase 1).

Renders the four checklist questions in their mandated order:
    1. Macro (level 1)          — ¿hay apetito por riesgo?
    2. Estado de cartera        — precios, variaciones, distancia al ATH
    3. Tesis / TVL (level 3)    — agrupado por categoría de tesis
    4. Ejecución (level 4)      — estado del plan DCA

Design constraints (sections 2, 11): this panel is PULL, not push. No auto-refresh,
no live tickers, no price animations. Data reflects the last ``run_ingest.py`` run;
the user refreshes deliberately.

Run:
    streamlit run app/dashboard.py

Reads: the SQLite database at Settings.db_path (schema ensured on load).
Writes: nothing.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the repo root importable when run as a bare script (e.g. Streamlit Cloud,
# which puts app/ on sys.path but not the project root, and does not install the
# package). Locally the editable install already covers this; the insert is a no-op.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import altair as alt
import pandas as pd
import streamlit as st

from core.config import load_settings
from db.loader import init_db
from db.queries import series_history, upcoming_events
from validation.backtest import funding_zscore_backtest
from transform.indicators import (
    capital_deployed_summary,
    dca_status,
    dca_vs_baseline_table,
    execution_summary,
    holdings_by_group,
    holdings_table,
    macro_table,
    portfolio_table,
    thesis_invalidation_table,
    thesis_tvl_table,
    value_accrual_table,
    wallet_pnl_history,
    wallet_pnl_table,
    wallet_value_history,
)
from transform.rally_quality import (
    RALLY_CAPITULATION,
    RALLY_CONVICTION,
    RALLY_DISTRIBUTION,
    RALLY_MECHANICAL,
    etf_flow_summary,
    market_structure_table,
)

# Rally-state -> (Spanish label, CSS color) for the market-structure table.
_RALLY_LABELS = {
    RALLY_CONVICTION: ("Convicción", "color:#16a34a;font-weight:600"),      # green
    RALLY_MECHANICAL: ("Mecánico (frágil)", "color:#eab308;font-weight:600"),  # amber
    RALLY_DISTRIBUTION: ("Distribución", "color:#dc2626;font-weight:600"),   # red
    RALLY_CAPITULATION: ("Capitulación", "color:#9ca3af;font-weight:600"),   # gray
}

# --- Formatting helpers ------------------------------------------------------


def _fmt_pct(x: float | None) -> str:
    """Format a percent value with sign, or an em dash when missing."""
    return "—" if x is None or pd.isna(x) else f"{x:+.2f}%"


def _fmt_usd(x: float | None) -> str:
    """Format a USD value with thousands separators, or an em dash when missing."""
    return "—" if x is None or pd.isna(x) else f"${x:,.2f}"


def _fmt_ratio(x: float | None) -> str:
    """Format a plain ratio to two decimals, or an em dash when missing."""
    return "—" if x is None or pd.isna(x) else f"{x:.2f}"


def _fmt_signed_usd(x: float | None) -> str:
    """Signed USD ('+$12.50' / '-$3.00' / '—') so _color_by_sign can tint it."""
    if x is None or pd.isna(x):
        return "—"
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _fmt_big_usd(x: float | None) -> str:
    """Format a large USD value compactly (K/M/B), or an em dash when missing."""
    if x is None or pd.isna(x):
        return "—"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.2f}"


def _fmt_date(iso: str | None) -> str:
    """Return just the ISO date (drop the time component), or an em dash."""
    return "—" if not iso or pd.isna(iso) else str(iso)[:10]


def _fmt_num(x: float | None) -> str:
    """Format a number with thousands separators, trimming trailing zeros."""
    if x is None or pd.isna(x):
        return "—"
    s = f"{x:,.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _color_by_sign(cell: object) -> str:
    """pandas Styler CSS: green for up, red for down, nothing otherwise.

    Operates on the formatted display string, which may start with an arrow
    ('▲ +2.50%', '▼ -17K') or a bare sign ('+0.35%', '-1.20%').
    """
    s = str(cell)
    if s.startswith("▲") or s.startswith("+"):
        return "color:#16a34a"  # green
    if s.startswith("▼") or s.startswith("-"):
        return "color:#dc2626"  # red
    return ""


def _fmt_change(x: float | None) -> str:
    """Format a percent change with a leading up/down/flat arrow, or an em dash."""
    if x is None or pd.isna(x):
        return "—"
    arrow = "▲" if x > 0 else "▼" if x < 0 else "▬"
    return f"{arrow} {x:+.2f}%"


def _fmt_pp(x: float | None) -> str:
    """Format a change in percentage points with an arrow, or an em dash."""
    if x is None or pd.isna(x):
        return "—"
    arrow = "▲" if x > 0 else "▼" if x < 0 else "▬"
    return f"{arrow} {x:+.2f} pp"


def _fmt_category(cat: str) -> str:
    """Thesis category as uppercase, underscores removed (defi_lending -> DEFI LENDING)."""
    return str(cat).replace("_", " ").upper()


def _fmt_kind(kind: str | None) -> str:
    """TVL source kind label: protocol -> Protocolo, chain -> Cadena, else em dash."""
    return {"protocol": "Protocolo", "chain": "Cadena"}.get(kind, "—")


def _color_dilution(cell: object) -> str:
    """pandas Styler CSS: yellow/bold when the dilution cell carries the ⚠ marker."""
    return "color:#eab308;font-weight:700" if "⚠" in str(cell) else ""


def _macro_change_value(record: dict) -> float | None:
    """The change magnitude of a macro row (absolute for NFP, else percent)."""
    if record.get("change_display") == "absolute":
        return record.get("change_abs")
    return record.get("change_pct")


def _macro_delta_str(record: dict) -> str:
    """Format the macro Δ with a leading arrow: '▲ +150K' (absolute) or '▲ +0.35%'."""
    value = _macro_change_value(record)
    if value is None or pd.isna(value):
        return "—"
    arrow = "▲" if value > 0 else "▼" if value < 0 else "▬"
    if record.get("change_display") == "absolute":
        return f"{arrow} {value:+,.0f}{record.get('change_unit', '')}"
    return f"{arrow} {value:+.2f}%"


def _sentiment(x: float) -> str:
    """Classify a change as bullish/bearish/neutral (Spanish, for hover tooltips)."""
    if x > 0:
        return "Alcista"
    if x < 0:
        return "Bajista"
    return "Neutral"


def _macro_effect_sentiment(value: float, crypto_effect: str | None) -> str:
    """Sentiment of a macro change *for crypto*, per the series' crypto_effect.

    'inverse' -> a rise in the indicator is bearish for crypto (CPI, PCE, NFP, rates,
    DXY); 'direct' -> a rise is bullish (e.g. a steepening yield curve). Falls back to
    a plain directional label when no effect is configured.
    """
    if value == 0:
        return "Neutral"
    if crypto_effect == "inverse":
        bullish = value < 0
    elif crypto_effect == "direct":
        bullish = value > 0
    else:
        return _sentiment(value)
    return "Alcista para cripto" if bullish else "Bajista para cripto"


def _fmt_tier(tier: str) -> str:
    """Format a tier: drop underscores, capitalize first letter (riesgo_medio -> Riesgo medio)."""
    return str(tier).replace("_", " ").capitalize()


def _dilution_tooltip(record: dict) -> str:
    """Tooltip for the dilution cell: circulating share, risk flag and next unlock.

    The unlock date is shown when available (populated by the Phase 2 unlocks
    ingester); otherwise it is marked pending rather than guessed.
    """
    dil = record.get("dilution_ratio")
    has_dil = dil is not None and not pd.isna(dil)  # None round-trips to NaN via DataFrame
    parts = [
        f"Circulante {dil * 100:.1f}% del máximo" if has_dil else "Suministro sin tope máximo"
    ]
    if record.get("dilution_risk"):
        parts.append("riesgo de dilución alto")
    nu = record.get("next_unlock")
    parts.append(f"próximo unlock: {nu}" if nu else "próximo unlock: pendiente (Fase 2)")
    return " · ".join(parts)


# --- Sections ----------------------------------------------------------------


def _section_macro(conn, settings) -> None:
    """Level 1 — macro context. Interactive grid (sort / reorder / hide columns)."""
    st.header("MACRO", anchor="macro")
    st.caption("¿Hay apetito por riesgo? — nivel 1 del checklist (sección 2).")
    df = macro_table(conn, settings)
    if df.empty or df["value"].notna().sum() == 0:
        st.info(
            "Sin datos macro. Configura `FRED_API_KEY` en `config/.env` y ejecuta "
            "`python run_ingest.py` para poblar CPI, PCE, NFP, Fed Funds, DXY y la curva."
        )
        return

    rows = []
    refs: list[tuple[str, str]] = []  # aligned with rows: (fred series_id, label)
    for r in df.to_dict("records"):
        if r["value"] is None or pd.isna(r["value"]):
            continue
        chg = _macro_change_value(r)
        signal = (
            "—"
            if chg is None or pd.isna(chg)
            else _macro_effect_sentiment(chg, r["crypto_effect"])
        )
        rows.append(
            {
                "Indicador": r["label"],
                "Último": _fmt_num(r["value"]),
                "Δ vs. previo": _macro_delta_str(r),
                "Señal cripto": signal,
                "Fecha ref.": _fmt_date(r["ts"]),
                "Publicado": _fmt_date(r["ts_release"]),
                "Descripción": r.get("description", ""),
            }
        )
        refs.append((r["series_id"], r["label"]))
    show = pd.DataFrame(rows)
    styler = show.style.map(_color_by_sign, subset=["Δ vs. previo"])
    event = st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        key="macro_tbl",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Δ vs. previo": st.column_config.TextColumn(
                help="Variación frente a la observación anterior; NFP en miles de empleos (+K)."
            ),
            "Señal cripto": st.column_config.TextColumn(
                help="Efecto del movimiento en cripto (alcista/bajista), no la dirección cruda."
            ),
            "Publicado": st.column_config.TextColumn(
                help="Fecha de primera publicación; los backtests filtran por ella (look-ahead, sección 9)."
            ),
            "Descripción": st.column_config.TextColumn(
                width="medium", help="Definición del indicador."
            ),
        },
    )
    st.caption(
        "Tabla interactiva: **clic en una fila para ver el historial** del indicador. "
        "Arrastra las cabeceras para reordenar y usa el menú de cada columna para ordenar "
        "u ocultar. `Señal cripto` traduce el movimiento a su efecto en cripto "
        "(CPI/PCE/NFP/Fed Funds/DXY inversos; curva directa)."
    )
    sel = _selected_row(event)
    if sel is not None:
        series_id, label = refs[sel]
        _drilldown_chart(conn, "fred", series_id, label)


def _render_btc_dominance(attrs: dict) -> None:
    """BTC dominance metric with importance help and 30d / 1y change (in pp)."""
    dominance = attrs.get("btc_dominance")
    if dominance is None:
        return
    is_global = attrs.get("btc_dominance_is_global", False)
    label = "Dominancia BTC (mercado total)" if is_global else "Dominancia BTC (universo seguido)"
    help_text = (
        "Cuota de BTC sobre la capitalización total de cripto. Subiendo = rotación hacia "
        "BTC y aversión al riesgo en altcoins; bajando = apetito por altcoins, entorno "
        "favorable a SOL/LINK (sección 2, nivel 2). Cambio en puntos porcentuales (pp)."
    )
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.metric(label, f"{dominance:.1f}%", help=help_text)
    c2.metric("vs. mes (30 d)", _fmt_pp(attrs.get("btc_dominance_chg_30d")))
    c3.metric("vs. año (365 d)", _fmt_pp(attrs.get("btc_dominance_chg_365d")))
    st.caption(
        "Variación de la dominancia en puntos porcentuales (pp). Muestra `—` hasta acumular "
        "30 días / 1 año de historial (CoinGecko gratuito no ofrece dominancia histórica)."
    )


def _section_portfolio(conn, settings) -> None:
    """Radar: interactive grid (sort / reorder / hide columns), logos, colored changes."""
    df = portfolio_table(conn, settings)

    # BTC dominance sits ABOVE the section title.
    _render_btc_dominance(df.attrs)

    st.header("2 · Radar", anchor="radar")
    if df.empty or df["price"].notna().sum() == 0:
        st.info("Sin datos de precio. Ejecuta `python run_ingest.py`.")
        return

    rows = []
    for r in df.to_dict("records"):
        if r["price"] is None or pd.isna(r["price"]):
            continue
        dil = _fmt_ratio(r["dilution_ratio"])
        if r.get("dilution_risk"):
            dil = f"⚠ {dil}"
        rows.append(
            {
                "Logo": r.get("logo_url") or "",
                "Activo": r["symbol"],
                "Tramo": _fmt_tier(r["tier"]),
                "Precio": _fmt_usd(r["price"]),
                "Cap. mercado": _fmt_big_usd(r["market_cap"]),
                "Vol. 24h": _fmt_big_usd(r["volume_24h"]),
                "24h": _fmt_change(r["chg_24h"]),
                "7d": _fmt_change(r["chg_7d"]),
                "30d": _fmt_change(r["chg_30d"]),
                "Dist. ATH": _fmt_change(r["dist_ath"]),
                "Dilución": dil,
                "Próx. unlock": r.get("next_unlock") or "pendiente (Fase 2)",
                "Tesis": r.get("description", ""),
            }
        )
    show = pd.DataFrame(rows)
    change_cols = ["24h", "7d", "30d", "Dist. ATH"]
    styler = (
        show.style
        .map(_color_by_sign, subset=change_cols)
        .map(_color_dilution, subset=["Dilución"])
    )
    st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Activo": st.column_config.TextColumn(help="Token seguido (sección 5)."),
            "Dist. ATH": st.column_config.TextColumn(help="Distancia al máximo histórico."),
            "Dilución": st.column_config.TextColumn(
                help="Circulante / suministro máximo. ⚠ = por debajo del umbral de dilución."
            ),
            "Próx. unlock": st.column_config.TextColumn(
                help="Próximo unlock del token (calendario en Fase 2)."
            ),
            "Tesis": st.column_config.TextColumn(
                width="medium", help="Tesis / métrica de invalidación (sección 5)."
            ),
        },
    )
    st.caption(
        "Tabla interactiva: arrastra para reordenar y usa el menú de columna para ordenar u "
        "ocultar. Verde = alcista, rojo = bajista en las variaciones; ⚠ en Dilución = "
        "circulante/máx por debajo del umbral. `Tesis` y `Próx. unlock` son ocultables. "
        "Las variaciones muestran `—` hasta acumular historial de precios."
    )


def _fmt_funding_z(z: float | None) -> str:
    """Funding z-score to 2 decimals, or an em dash."""
    return "—" if z is None or pd.isna(z) else f"{z:+.2f}"


def _color_funding_z(cell: object) -> str:
    """Styler CSS: red/bold when |z| >= 2 (crowded positioning, sección 8)."""
    try:
        z = float(str(cell))
    except ValueError:
        return ""
    return "color:#dc2626;font-weight:700" if abs(z) >= 2 else ""


def _rally_label(state: str | None) -> str:
    """Spanish label for a rally-state code, or an em dash."""
    return "—" if state is None else _RALLY_LABELS.get(state, (state, ""))[0]


def _color_rally(cell: object) -> str:
    """Styler CSS for a rally-state label cell."""
    for label, css in _RALLY_LABELS.values():
        if str(cell) == label:
            return css
    return ""


def _upcoming_unlocks(settings, within_days: int = 30) -> list[tuple[str, str, int]]:
    """Return [(symbol, date, days_until)] for configured next_unlock within the window.

    Reads config/assets_meta.yaml (manual dates, sección 5). Empty until dates are filled in.
    """
    today = datetime.now(timezone.utc).date()
    out: list[tuple[str, str, int]] = []
    for symbol, meta in settings.asset_meta.items():
        raw = meta.get("next_unlock") if isinstance(meta, dict) else None
        if not raw:
            continue
        try:
            date = datetime.strptime(str(raw), "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (date - today).days
        if 0 <= days <= within_days:
            out.append((symbol, str(raw), days))
    return sorted(out, key=lambda item: item[2])


def _events_strip(conn, settings) -> None:
    """Top strip: high-impact macro releases + FOMC + token unlocks in the next 7 days.

    Answers "¿hay un release/unlock en los próximos 7 días?" (checklist niveles 1 y 4):
    si lo hay, considerar posponer el tramo de DCA. Estático, de consulta semanal.
    """
    today = datetime.now(timezone.utc).date()
    # (days_until, date, material-icon, label, note)
    items: list[tuple[int, str, str, str, str]] = []
    for e in upcoming_events(conn, within_days=7, categories=("macro",)):
        items.append((e["days_until"], e["date"], ":material/calendar_month:", e["label"], "release macro"))
    for raw in settings.raw.get("macro_calendar", {}).get("fomc_dates", []) or []:
        try:
            d = datetime.strptime(str(raw), "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (d - today).days
        if 0 <= days <= 7:
            items.append((days, str(raw), ":material/account_balance:", "FOMC", "decisión de tipos"))
    for sym, date, days in _upcoming_unlocks(settings, within_days=7):
        items.append((days, date, ":material/lock_open:", f"Unlock {sym}", "desbloqueo de tokens"))

    st.subheader("Próximos 7 días", anchor="eventos")
    if not items:
        st.caption(
            "Sin releases macro, FOMC ni unlocks en los próximos 7 días — nada que aconseje "
            "posponer el tramo de DCA (niveles 1 y 4)."
        )
        return
    items.sort(key=lambda x: x[0])
    shown = items[:6]
    for col, (days, date, icon, label, note) in zip(st.columns(len(shown)), shown, strict=False):
        when = "hoy" if days == 0 else ("mañana" if days == 1 else f"en {days} d")
        color = "red" if days <= 1 else "orange" if days <= 3 else "green"
        with col.container(border=True):
            st.markdown(f"{icon} **{label}**")
            st.markdown(f":{color}[{when}] · {date}")
            st.caption(note)
    tail = f" (+{len(items) - 6} más)" if len(items) > 6 else ""
    st.caption(
        "Si hay un release de alto impacto en <7 días, considera **posponer** el tramo de DCA "
        f"(niveles 1 y 4). CPI/PCE/NFP vía FRED; FOMC y unlocks por configuración.{tail}"
    )


def _section_market_structure(conn, settings) -> None:
    """Level 2 — market structure: ETF flows, rally quality, funding, unlocks."""
    st.header("3 · Estructura de mercado", anchor="estructura")
    st.caption("¿El rally tiene sustento o es mecánico? — nivel 2 del checklist (sección 2).")

    # ETF flows: streak of positive/negative days + 5-day sum, per asset.
    etf = etf_flow_summary(conn, settings)
    if not etf.empty:
        cols = st.columns(len(etf))
        for col, r in zip(cols, etf.to_dict("records")):
            direction = "entradas" if r["streak_sign"] > 0 else "salidas" if r["streak_sign"] < 0 else "—"
            col.metric(
                f"{r['asset']} ETF — racha",
                f"{r['streak_days']} d de {direction}",
                delta=f"5 d: {r['sum_5d']:+,.0f} M",
                delta_color="off",
                help="Flujo neto diario de ETF spot (Farside, millones USD). Racha = días "
                "consecutivos del mismo signo, ignorando días sin dato.",
            )
    else:
        st.info("Sin datos de flujos ETF. Ejecuta `python run_ingest.py`.")

    # Rally quality per asset: state, funding z-score, price/OI divergence.
    ms = market_structure_table(conn, settings)
    if not ms.empty:
        rows = [
            {
                "Logo": settings.meta_for(r["symbol"]).get("logo_url") or "",
                "Activo": r["symbol"],
                "Estado": _rally_label(r["rally_state"]),
                "Funding z": _fmt_funding_z(r["funding_z"]),
                "Precio 7d": _fmt_change(r["price_chg"]),
                "OI 7d": _fmt_change(r["oi_chg"]),
                "OI 30d": _fmt_change(r["oi_chg_30d"]),
            }
            for r in ms.to_dict("records")
        ]
        show = pd.DataFrame(rows)
        styler = (
            show.style
            .map(_color_rally, subset=["Estado"])
            .map(_color_funding_z, subset=["Funding z"])
            .map(_color_by_sign, subset=["Precio 7d", "OI 7d", "OI 30d"])
        )
        st.dataframe(
            styler,
            hide_index=True,
            width="stretch",
            column_config={
                "Logo": st.column_config.ImageColumn("", width="small"),
                "Estado": st.column_config.TextColumn(
                    help="Precio↑ OI↑ = Convicción (dinero nuevo); Precio↑ OI↓ = Mecánico "
                    "(cierre de cortos, frágil); Precio↓ OI↑ = Distribución; Precio↓ OI↓ = "
                    "Capitulación. Medido en el perp (7 días)."
                ),
                "Funding z": st.column_config.TextColumn(
                    help="Z-score del funding sobre 90 días. |z|≥2 (rojo) = posicionamiento "
                    "hacinado: z>2 largos (riesgo de cascada), z<-2 cortos (short squeeze)."
                ),
                "OI 7d": st.column_config.TextColumn(help="Variación del interés abierto (USD) a 7 días."),
                "OI 30d": st.column_config.TextColumn(
                    help="Variación del interés abierto (USD) a 30 días (~límite del historial de OI)."
                ),
            },
        )
        st.caption(
            "Clasificación del rally por divergencia precio/OI y funding z-score (sección 2 nivel 2, sección 8). "
            "Verde/rojo en Precio/OI = dirección; Funding z en rojo = extremo (≥2)."
        )
    else:
        st.info("Sin datos de derivados. Ejecuta `python run_ingest.py` (extra `.[markets]`).")

    # Upcoming unlocks (manual config, sección 5: revisar siempre).
    unlocks = _upcoming_unlocks(settings, within_days=30)
    if unlocks:
        st.write(
            "**Próximos unlocks (≤30 d):** "
            + " · ".join(f"{sym} {date} ({days} d)" for sym, date, days in unlocks)
        )
    else:
        st.caption(
            "Próximos unlocks: ninguno configurado. Añade `next_unlock: \"YYYY-MM-DD\"` por "
            "token en `config/assets_meta.yaml` (sección 5: revisar siempre, sin excepción)."
        )


def _section_thesis(conn, settings) -> None:
    """Level 3 — thesis health. All tokens, grouped by category; interactive grid."""
    st.header("4 · Tesis — TVL por categoría", anchor="tesis")
    df = thesis_tvl_table(conn, settings)
    if df.empty:
        st.info("Sin activos configurados en `settings.yaml`.")
        return

    cat_notes = settings.raw.get("thesis_categories", {})
    slug_by_symbol = {
        a["symbol"]: a["defillama"]["slug"] for a in settings.assets if a.get("defillama")
    }
    rows = []
    refs: list[tuple[str, str, str] | None] = []  # aligned: (source, series_id, title) or None
    for r in df.to_dict("records"):
        rows.append(
            {
                "Logo": r.get("logo_url") or "",
                "Activo": r["symbol"],
                "Categoría tesis": _fmt_category(r["thesis_category"]),
                "Tipo": _fmt_kind(r["kind"]),
                "TVL": _fmt_big_usd(r["tvl"]),
                "TVL 7d": _fmt_change(r["tvl_chg_7d"]),
                "TVL 30d": _fmt_change(r["tvl_chg_30d"]),
                "MC/TVL": _fmt_ratio(r["mc_tvl"]),
                "Tesis": r.get("description", ""),
                "Nota categoría": cat_notes.get(r["thesis_category"], ""),
            }
        )
        slug = slug_by_symbol.get(r["symbol"])
        refs.append(("defillama", f"{slug}:tvl", f"TVL {r['symbol']}") if slug else None)
    show = pd.DataFrame(rows)
    styler = show.style.map(_color_by_sign, subset=["TVL 7d", "TVL 30d"])
    event = st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        key="thesis_tbl",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Categoría tesis": st.column_config.TextColumn(
                help="Agrupación por modo de fallo / megatendencia. Diversificar por "
                "categoría, no por ticker (sección 5). Ver la columna 'Nota categoría'."
            ),
            "Tipo": st.column_config.TextColumn(
                help="Origen del TVL: Protocolo = app DeFi (DefiLlama /protocol); "
                "Cadena = L1 completa (DefiLlama /chain); — = sin TVL rastreado."
            ),
            "TVL": st.column_config.TextColumn(
                help="Valor total bloqueado (DefiLlama). — para activos sin TVL "
                "rastreado, p. ej. BTC, XRP, TAO."
            ),
            "MC/TVL": st.column_config.TextColumn(
                help="Capitalización / TVL. Alto = precio caro respecto a la actividad "
                "capturada (value accrual, sección 2)."
            ),
            "Tesis": st.column_config.TextColumn(
                width="medium", help="Tesis / métrica de invalidación (sección 5)."
            ),
            "Nota categoría": st.column_config.TextColumn(
                width="medium", help="Explicación breve de la categoría de tesis."
            ),
        },
    )
    st.caption(
        "Todos los activos, agrupados por *categoría de tesis* para exponer concentración "
        "disfrazada de diversificación (sección 5). **Clic en una fila para ver el historial de TVL** "
        "(activos sin TVL rastreado no muestran gráfico). Verde/rojo = variación de TVL."
    )
    sel = _selected_row(event)
    if sel is not None:
        ref = refs[sel]
        if ref is None:
            st.info("Este activo no tiene TVL rastreado (p. ej. BTC, XRP, TAO): sin historial que mostrar.")
        else:
            _drilldown_chart(conn, ref[0], ref[1], ref[2])

    _thesis_invalidation_board(conn, settings)
    _value_accrual_view(conn, settings)


_INVALIDATION_LABEL = {"red": "🔴 Riesgo", "amber": "🟠 Vigilar", "green": "🟢 OK", "na": "⚪ s/d"}


def _color_invalidation(cell: object) -> str:
    """Styler CSS for the invalidation status cell (red/amber/green/gray)."""
    s = str(cell)
    if "Riesgo" in s:
        return "color:#dc2626;font-weight:600"
    if "Vigilar" in s:
        return "color:#eab308;font-weight:600"
    if "OK" in s:
        return "color:#16a34a;font-weight:600"
    return "color:#9ca3af"


def _thesis_invalidation_board(conn, settings) -> None:
    """Green/amber/red board of each asset's thesis-invalidation status (level 3)."""
    df = thesis_invalidation_table(conn, settings)
    if df.empty:
        return
    st.subheader("Tablero de invalidación de tesis")
    show = pd.DataFrame(
        {
            "Logo": [r.get("logo_url") or "" for r in df.to_dict("records")],
            "Activo": df["symbol"],
            "Estado": df["status"].map(_INVALIDATION_LABEL),
            "Motivo": df["reason"],
            "Métrica de invalidación": df["invalidation"],
        }
    )
    styler = show.style.map(_color_invalidation, subset=["Estado"])
    st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Estado": st.column_config.TextColumn(
                help="Semáforo a partir de señales cuantitativas: salidas ETF sostenidas "
                "(BTC/ETH), caída de TVL 7d, dilución alta (circ/máx), y unlock próximo. "
                "Toma la peor señal aplicable."
            ),
            "Métrica de invalidación": st.column_config.TextColumn(
                width="large", help="Qué rompería la tesis del activo (nivel 3)."
            ),
        },
    )
    st.caption(
        "Semáforo de la tesis por activo (nivel 3): 🔴 una señal de invalidación cruzó umbral · "
        "🟠 vigilar · 🟢 sin alerta · ⚪ métrica **cualitativa**, no medible con estos datos "
        "(p. ej. demanda de token de HBAR, riesgo regulatorio de BNB). Basado en salidas ETF "
        "(BTC/ETH), TVL, dilución y unlocks; no captura invalidaciones cualitativas."
    )


def _value_accrual_view(conn, settings) -> None:
    """Scatter + ranking of activity (TVL) vs. valuation (mcap) — value accrual (§2)."""
    df = value_accrual_table(conn, settings)
    if df.empty:
        return
    st.subheader("Value accrual — actividad (TVL) vs. valoración (mcap)")
    # Altair 6.2 can't serialize a pandas-3.0 DataFrame -> feed records via alt.Data.
    records = [
        {
            "symbol": r["symbol"],
            "tvl": r["tvl"],
            "mcap": r["mcap"],
            "mc_tvl": round(r["mc_tvl"], 3) if pd.notna(r["mc_tvl"]) else None,
            # None -> NaN in a DataFrame; convert back so Altair gets valid JSON null.
            "revenue_ann": r["revenue_ann"] if pd.notna(r.get("revenue_ann")) else None,
            "mc_revenue": round(r["mc_revenue"], 1) if pd.notna(r.get("mc_revenue")) else None,
        }
        for r in df.to_dict("records")
    ]
    data = alt.Data(values=records)
    # High MC/TVL = valuation ahead of activity -> red; low = cheap -> green.
    color = alt.Color(
        "mc_tvl:Q", scale=alt.Scale(scheme="redyellowgreen", reverse=True), title="MC/TVL"
    )
    tooltip = [
        alt.Tooltip("symbol:N", title="Activo"),
        alt.Tooltip("tvl:Q", title="TVL", format=",.0f"),
        alt.Tooltip("mcap:Q", title="Mcap", format=",.0f"),
        alt.Tooltip("mc_tvl:Q", title="MC/TVL", format=".2f"),
        alt.Tooltip("revenue_ann:Q", title="Revenue anual", format=",.0f"),
        alt.Tooltip("mc_revenue:Q", title="MC/Revenue", format=".1f"),
    ]
    pts = alt.Chart(data).mark_circle(size=160, opacity=0.85).encode(
        x=alt.X("tvl:Q", scale=alt.Scale(type="log"), title="TVL (USD, escala log)"),
        y=alt.Y("mcap:Q", scale=alt.Scale(type="log"), title="Capitalización (USD, escala log)"),
        color=color,
        tooltip=tooltip,
    )
    labels = alt.Chart(data).mark_text(dy=-13, fontSize=11).encode(
        x=alt.X("tvl:Q", scale=alt.Scale(type="log")),
        y=alt.Y("mcap:Q", scale=alt.Scale(type="log")),
        text="symbol:N",
    )
    bar = alt.Chart(data).mark_bar().encode(
        x=alt.X("mc_tvl:Q", title="MC/TVL (menor = más barato vs. actividad)"),
        y=alt.Y("symbol:N", sort="x", title=None),
        color=color.copy(),
        tooltip=tooltip,
    )
    c1, c2 = st.columns([3, 2])
    c1.altair_chart((pts + labels).properties(height=360), width="stretch")
    c2.altair_chart(bar.properties(height=360), width="stretch")

    # MC/Revenue = crypto P/E (valuation vs. revenue the token actually captures).
    ranked = sorted(
        (r for r in df.to_dict("records") if pd.notna(r.get("mc_revenue"))),
        key=lambda r: r["mc_revenue"],
    )
    zero_rev = [r["symbol"] for r in df.to_dict("records") if r.get("revenue_ann") == 0]
    rev_line = ""
    if ranked:
        rev_line = " · **MC/Revenue** (P/E cripto, menor = más barato): " + " · ".join(
            f"{r['symbol']} {r['mc_revenue']:,.0f}×" for r in ranked
        )
    if zero_rev:
        rev_line += (
            f" · **{', '.join(zero_rev)}: $0 de revenue al token** pese a su TVL → "
            "sin *value accrual* (la mejor señal de tesis rota)."
        )
    st.caption(
        "El *concepto rector* (sección 2): ¿el precio sigue a la actividad que el protocolo "
        "captura? **MC/TVL alto (rojo)** = valoración por delante de la actividad on-chain (cara); "
        "**bajo (verde)** = barata. Aún más directo: **MC/Revenue** compara la valoración con el "
        "*revenue* que llega al token (DefiLlama, anualizado desde 30 d)." + rev_line
    )


def _donut(df: pd.DataFrame, title: str) -> alt.Chart | None:
    """Allocation donut from a [group, value_usd, weight_pct] frame (None if empty).

    Static, weekly-glance figure (sección 2): no animation, no live refresh.
    """
    if df is None or df.empty:
        return None
    # Altair 6.2 cannot serialize a pandas-3.0 DataFrame (it inlines zero rows -> blank
    # chart). Feed pre-converted records via alt.Data so the values inline correctly;
    # this requires explicit field types (:Q/:N) in every encoding, which we set below.
    records = df.to_dict("records")
    return (
        alt.Chart(alt.Data(values=records))
        .mark_arc(innerRadius=55)
        .encode(
            theta=alt.Theta("value_usd:Q", stack=True),
            color=alt.Color("group:N", legend=alt.Legend(title=None, orient="bottom")),
            order=alt.Order("value_usd:Q", sort="descending"),
            tooltip=[
                alt.Tooltip("group:N", title="Grupo"),
                alt.Tooltip("value_usd:Q", title="USD", format=",.0f"),
                alt.Tooltip("weight_pct:Q", title="Peso", format=".1f"),
            ],
        )
        .properties(title=title, height=300)
    )


def _selected_row(event) -> int | None:
    """Return the positional index of the single selected row, or None.

    Works with st.dataframe(on_select="rerun", selection_mode="single-row"); the
    selection is returned in the data's original order, independent of client sorting.
    """
    try:
        rows = event.selection.rows
    except AttributeError:
        return None
    return rows[0] if rows else None


def _drilldown_chart(conn, source: str, series_id: str, title: str) -> None:
    """Line chart of one stored series' daily history (sección 13 #3).

    Daily resolution only, no intraday (sección 11). Shows a note when history is too thin
    to plot (it accumulates with each ``run_ingest.py``).
    """
    hist = series_history(conn, source, series_id).dropna()
    if hist.empty:
        st.info(f"Sin historial almacenado para **{title}** todavía.")
        return
    if len(hist) < 2:
        st.info(
            f"Solo un punto de **{title}**; el historial se acumula con cada "
            "`run_ingest.py` (resolución diaria, sección 11)."
        )
        return
    data = hist.rename(title).to_frame()
    data.index.name = "fecha"
    st.line_chart(data, height=260)
    st.caption(f"Historial diario de **{title}** — {len(hist)} puntos. Vuelve a hacer clic para cerrar.")


def _fmt_amount(a: float | None) -> str:
    """Format a token amount with up to 8 decimals, trailing zeros trimmed."""
    if a is None or pd.isna(a):
        return "—"
    s = f"{a:,.8f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _section_execution(conn, settings) -> None:
    """Level 4 — real account (read-only) + DCA plan."""
    st.header("5 · Ejecución — cartera real y plan DCA", anchor="ejecucion")

    # --- Real account (read-only Binance sync) -------------------------------
    st.subheader("Cartera real (Binance, solo lectura)")
    holdings = holdings_table(conn, settings)
    if holdings.empty:
        st.info(
            "Cuenta no conectada. Crea una API key **de solo lectura** en Binance "
            "(sin trading ni retiros, restringida por IP), añádela como "
            "`BINANCE_API_KEY` / `BINANCE_API_SECRET` en `config/.env` y ejecuta "
            "`python run_ingest.py`. Ver `config/.env.example` para los permisos exactos."
        )
    else:
        exec_sum = execution_summary(conn, settings)
        total = holdings.attrs.get("total_value_usd", 0.0)
        cash = holdings.attrs.get("cash_usd", 0.0)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Valor cartera", _fmt_usd(total))
        c2.metric("Efectivo (stables)", _fmt_usd(cash))
        if exec_sum.get("has_trades"):
            c3.metric("Invertido neto", _fmt_usd(exec_sum["net_invested_usd"]))
            c4.metric(
                "Comisiones reales",
                _fmt_usd(exec_sum["fees_usd"]),
                help=(
                    "Comisiones de operaciones convertidas a USD (stablecoins a $1, resto vía "
                    "precio). "
                    + (
                        f"{exec_sum['fees_unconverted']} comisión(es) sin convertir."
                        if exec_sum.get("fees_unconverted")
                        else "Todas convertidas."
                    )
                ),
            )
        show = pd.DataFrame(
            {
                "Logo": [settings.meta_for(a).get("logo_url") or "" for a in holdings["asset"]],
                "Activo": holdings["asset"],
                "Cantidad": holdings["amount"].map(_fmt_amount),
                "Precio": holdings["price_usd"].map(_fmt_usd),
                "Valor": holdings["value_usd"].map(_fmt_usd),
                "Peso": holdings["weight_pct"].map(
                    lambda w: "—" if w is None or pd.isna(w) else f"{w:.1f}%"
                ),
                "Nota": holdings["note"],
            }
        )
        st.dataframe(
            show,
            hide_index=True,
            width="stretch",
            column_config={"Logo": st.column_config.ImageColumn("", width="small")},
        )
        wallets = ", ".join(settings.source("binance_account").get("wallets", ["spot"]))
        st.caption(
            f"Balances sumados entre wallets: **{wallets}** (config `binance_account.wallets`; "
            "futuros off por defecto, sección 11). Solo lectura: el panel nunca opera ni retira (sección 2, sección 11). "
            "El registro real de comisiones permite comparar contra el baseline de mantener."
        )

        # Allocation donuts: concentration by thesis category (sección 5) + weight by asset.
        by_cat = holdings_by_group(holdings, settings, by="thesis_category")
        by_asset = holdings_by_group(holdings, settings, by="asset")
        ch_cat = _donut(by_cat, "Asignación por categoría de tesis")
        ch_asset = _donut(by_asset, "Asignación por activo")
        if ch_cat is not None or ch_asset is not None:
            g1, g2 = st.columns(2)
            if ch_cat is not None:
                g1.altair_chart(ch_cat, width="stretch")
            if ch_asset is not None:
                g2.altair_chart(ch_asset, width="stretch")
            st.caption(
                "Donut por **categoría de tesis**: hace visible la concentración disfrazada de "
                "diversificación (sección 5) — varias posiciones RWA (LINK/ONDO/XLM/HBAR) cuentan como una "
                "sola apuesta. `Efectivo` = stablecoins; `Otros` = activos fuera de sección 5 (p. ej. WBETH)."
            )

    # --- PnL per token + hold-simulation history -----------------------------
    _wallet_pnl_view(conn, settings, holdings)

    # --- DCA plan (manual) ---------------------------------------------------
    st.subheader("Plan DCA")
    status = dca_status(conn, settings)
    c1, c2, c3 = st.columns(3)
    c1.metric("Desplegado (plan)", _fmt_usd(status["deployed_usd"]))
    c2.metric("Planificado", _fmt_usd(status["planned_usd"]))
    c3.metric("Comisiones (plan)", _fmt_usd(status["fees_usd"]))

    nxt = status["next_tranche"]
    if nxt is None:
        st.info(
            "No hay tramos pendientes en `dca_plan`. El plan DCA se carga en esta tabla "
            f"(aporte mensual mínimo: {_fmt_usd(status['monthly_min_usd'])})."
        )
    else:
        st.write(
            f"**Próximo tramo:** {nxt['asset']} ({nxt['tier']}) · "
            f"{_fmt_usd(nxt['amount_usd'])} · objetivo {nxt['target_date']}"
        )

    _dca_baseline_view(conn, settings)


def _wallet_pnl_view(conn, settings, holdings) -> None:
    """Per-token unrealized PnL + hold-simulation value/PnL history (level 4)."""
    df = wallet_pnl_table(conn, settings, holdings)
    st.subheader("PnL por activo")
    if df.empty:
        st.caption("Sin holdings valorables. Sincroniza la cuenta (`python run_ingest.py`).")
        return

    tv = df.attrs.get("total_value_usd", 0.0)
    tc = df.attrs.get("total_cost_usd", 0.0)
    tp = df.attrs.get("total_pnl_usd", 0.0)
    c1, c2, c3 = st.columns(3)
    c1.metric("Valor (sin efectivo)", _fmt_usd(tv))
    c2.metric("Coste (posiciones con base)", _fmt_usd(tc))
    c3.metric("PnL no realizado", _fmt_signed_usd(tp), delta=f"{tp / tc * 100:+.1f}%" if tc else None)

    exec_sum = execution_summary(conn, settings)
    drag = exec_sum.get("fees_drag_pct") if exec_sum.get("has_trades") else None
    if drag is not None:
        per = exec_sum.get("fees_per_trade_usd")
        st.caption(
            f"**Arrastre de comisiones:** {_fmt_usd(exec_sum['fees_usd'])} en "
            f"{exec_sum['n_trades']} operaciones = **{drag:.2f}%** del capital desplegado"
            + (f" ({_fmt_usd(per)}/operación)" if per is not None else "")
            + ". Lastre material con tickets pequeños (secciones 1, 4): cada compra paga comisión, "
            "así que muchas compras pequeñas erosionan más que pocas grandes."
        )

    cap = capital_deployed_summary(conn)
    if cap.get("has_flows"):
        total_val = holdings.attrs.get("total_value_usd", 0.0)  # incl. efectivo
        net = cap["net_deployed_usd"]
        roi = (total_val / net - 1) * 100 if net else None
        k1, k2, k3 = st.columns(3)
        k1.metric("Capital neto aportado", _fmt_usd(net), help="Depósitos − retiros fiat (USD).")
        k2.metric("Valor total (incl. efectivo)", _fmt_usd(total_val))
        k3.metric(
            "Retorno sobre aportes", "—" if roi is None else f"{roi:+.1f}%",
            help="Valor total de la cuenta ÷ capital neto aportado − 1. La rentabilidad real "
            "sobre lo que has ingresado (secciones 1, 2).",
        )
        if cap["unconverted"]:
            st.caption(
                f"{cap['unconverted']} movimiento(s) fiat sin tasa FX → excluidos del cómputo. "
                "Añade `fx_to_usd` (p. ej. COP) en `config/settings.local.yaml` para incluirlos."
            )

    show = pd.DataFrame(
        {
            "Logo": [settings.meta_for(s).get("logo_url") or "" for s in df["symbol"]],
            "Activo": df["symbol"],
            "Cantidad": df["amount"].map(_fmt_amount),
            "Entrada media": df["avg_price"].map(_fmt_usd),
            "Valor": df["value_usd"].map(_fmt_usd),
            "Coste": df["cost_usd"].map(_fmt_usd),
            "PnL": df["pnl_usd"].map(_fmt_signed_usd),
            "PnL %": df["pnl_pct"].map(_fmt_change),
            "Base": df["source"].map(lambda s: s if s else "—"),
        }
    )
    styler = show.style.map(_color_by_sign, subset=["PnL", "PnL %"])
    st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Entrada media": st.column_config.TextColumn(
                help="Precio medio de entrada. `trades` = de tus operaciones reales; `manual` = "
                "de `cost_basis` en config/settings.local.yaml (para tokens sin operaciones)."
            ),
            "Base": st.column_config.TextColumn(help="Origen del coste: trades | manual | — (sin coste)."),
        },
    )
    missing = list(df[df["cost_usd"].isna()]["symbol"])
    if missing:
        st.caption(
            f"Sin coste para **{', '.join(missing)}** — añade `cost_basis` en "
            "`config/settings.local.yaml` (ver `.example`) para calcular su PnL."
        )

    g1, g2 = st.columns(2)
    with g1:
        st.markdown("**Valor de la cartera (histórico)**")
        vh = wallet_value_history(conn, settings, holdings)
        if vh.empty:
            st.caption("Sin histórico de precios (ejecuta `python run_ingest.py --backfill`).")
        else:
            st.line_chart(vh.rename("Valor USD"), height=260)
    with g2:
        st.markdown("**PnL no realizado (histórico)**")
        ph = wallet_pnl_history(conn, settings, holdings)
        if ph.empty:
            st.caption("Sin coste conocido para ninguna posición (añade `cost_basis`).")
        else:
            st.line_chart(ph.rename("PnL USD"), height=260)
    st.caption(
        "Los gráficos valoran tus tenencias **actuales** a precios históricos (simulación de "
        "*mantener*): **no** es el valor real que tuviste en el pasado — los snapshots de balance "
        "solo empiezan al conectar la cuenta. Diario, sin intradía (sección 11)."
    )


def _dca_baseline_view(conn, settings) -> None:
    """DCA-vs-baseline tracker: did real entry timing beat blindly averaging in? (§2)."""
    df = dca_vs_baseline_table(conn, settings)
    st.subheader("DCA real vs. baseline")
    if df.empty:
        st.caption(
            "Sin operaciones de compra registradas para comparar. Se puebla al sincronizar "
            "la cuenta (`run_ingest.py`)."
        )
        return
    inv = df.attrs.get("total_invested_usd", 0.0)
    val = df.attrs.get("total_value_now_usd", 0.0)
    c1, c2 = st.columns(2)
    c1.metric("Invertido (compras netas)", _fmt_usd(inv))
    c2.metric(
        "Valor actual", _fmt_usd(val),
        delta=f"{(val / inv - 1) * 100:+.1f}%" if inv else None,
    )
    show = pd.DataFrame(
        {
            "Logo": [settings.meta_for(s).get("logo_url") or "" for s in df["symbol"]],
            "Activo": df["symbol"],
            "Compras": df["n_buys"],
            "Entrada media": df["avg_entry"].map(_fmt_usd),
            "Precio actual": df["current_price"].map(_fmt_usd),
            "Retorno real": df["actual_ret_pct"].map(_fmt_change),
            "Baseline (media)": df["baseline_avg"].map(_fmt_usd),
            "Retorno baseline": df["baseline_ret_pct"].map(_fmt_change),
            "Edge": df["edge_pp"].map(_fmt_pp),
        }
    )
    styler = show.style.map(
        _color_by_sign, subset=["Retorno real", "Retorno baseline", "Edge"]
    )
    st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Entrada media": st.column_config.TextColumn(
                help="Precio medio ponderado de tus compras reales."
            ),
            "Baseline (media)": st.column_config.TextColumn(
                help="Precio medio diario en la ventana de acumulación = comprar a cadencia fija "
                "(DCA ciego). — hasta que haya histórico que cubra la ventana (usa --backfill)."
            ),
            "Edge": st.column_config.TextColumn(
                help="Retorno real − retorno baseline. Positivo = tu *timing* batió el promediar "
                "a ciegas; negativo = lo empeoró."
            ),
        },
    )
    st.caption(
        "¿El *timing* de tus entradas batió a promediar a ciegas? (sección 2, objetivo conductual). "
        "**Edge > 0** = compraste por debajo del precio medio de la ventana. El baseline necesita "
        "histórico de precios que cubra la ventana; se rellena con `python run_ingest.py --backfill`."
    )


@st.cache_data(ttl=3600, show_spinner="Validando señales (backtest)…")
def _validation_rows(db_path_str: str, z: float, horizon: int) -> list[dict]:
    """Funding z-score backtest per asset → honest edge rows (sección 8). Cached ~1h (~3s cold).

    Reopens its own connection so the result is a plain serializable list (cacheable).
    Works on both backends: init_db routes to Neon when DATABASE_URL is set.
    """
    settings = load_settings()
    conn = init_db(Path(db_path_str))
    try:
        out: list[dict] = []
        for asset in [a["symbol"] for a in settings.assets]:
            res = funding_zscore_backtest(conn, settings, asset, z_threshold=z, horizons=(horizon,))
            if res is None:
                continue
            stats = res[horizon]
            if stats["n_signal"] == 0:
                continue
            out.append(
                {
                    "asset": asset,
                    "n": stats["n_signal"],
                    "signal": stats["mean_signal"],
                    "baseline": stats["mean_baseline"],
                    "edge": stats["edge"],
                    "pvalue": stats["pvalue"],
                }
            )
        out.sort(key=lambda r: (r["edge"] is None, r["edge"]))  # most-negative edge first
        return out
    finally:
        conn.close()


def _section_validation(settings) -> None:
    """Read-only showcase of the honest signal-validation table (sección 8, sección 13 #4).

    Public-safe: computed from public price/funding data (no account), so it renders on
    the public deploy too. This is a *method* showcase and an honest result, not advice.
    """
    st.header("Validación de señales", anchor="validacion")
    st.caption(
        "¿La señal tiene *edge*? Backtest honesto (sección 8), no una recomendación. Señal probada: "
        "funding z-score ≥ 1.0 (ventana 90 d, *point-in-time*, sin look-ahead sección 9) → retorno a "
        "7 d del cierre del perp, contra baseline de todas las fechas, p-valor por bootstrap."
    )
    rows = _validation_rows(str(settings.db_path), 1.0, 7)
    if not rows:
        st.info(
            "Sin fechas de señal todavía (historial de funding/close insuficiente). "
            "La tabla se puebla al acumular datos; reejecuta al pasar semanas."
        )
        return

    def _signed(x: float | None) -> str:
        return "—" if x is None else f"{x:+.2f}"

    show = pd.DataFrame(
        {
            "Logo": [settings.meta_for(r["asset"]).get("logo_url") or "" for r in rows],
            "Activo": [r["asset"] for r in rows],
            "n": [r["n"] for r in rows],
            "Señal %": [_signed(r["signal"]) for r in rows],
            "Base %": [_signed(r["baseline"]) for r in rows],
            "Edge (pp)": [_signed(r["edge"]) for r in rows],
            "p": ["—" if r["pvalue"] is None else f"{r['pvalue']:.3f}" for r in rows],
        }
    )
    # Color the Edge column by sign: green = signal beat baseline, red = weaker
    # (the mostly-red result is coherent with the 'crowded longs -> correction' thesis).
    styler = show.style.map(_color_by_sign, subset=["Edge (pp)"])
    st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "n": st.column_config.TextColumn(help="Nº de fechas con señal (muestra)."),
            "Señal %": st.column_config.TextColumn(
                help="Retorno medio a 7 d tras la señal."
            ),
            "Base %": st.column_config.TextColumn(
                help="Retorno medio a 7 d sobre todas las fechas (baseline)."
            ),
            "Edge (pp)": st.column_config.TextColumn(
                help="Señal − Base, en puntos porcentuales. Negativo = la señal precede a "
                "retornos más débiles (coherente con 'largos hacinados → corrección')."
            ),
            "p": st.column_config.TextColumn(
                help="p-valor por bootstrap (permutación). <0.05 = poco probable por azar, "
                "pero con muestras pequeñas es poco potente."
            ),
        },
    )
    st.caption(
        "**Nota honesta:** muestras pequeñas (~30–90 d), p-valores poco potentes y *multiple "
        "testing* (varios activos → falsos positivos esperables). **Preliminar, no accionable.** "
        "Documentar el resultado aunque no haya edge es parte del proyecto (sección 8); se reejecuta al "
        "acumular historial. Cacheado 1 h."
    )


def _sidebar_nav(settings) -> None:
    """Anchor navigation to jump between sections.

    Links follow the mandated level 1->4 order (sección 2): Macro stays first so the hierarchy
    (macro overrides thesis) is always visible. The level-4 link is hidden in public mode,
    where that section is not rendered.
    """
    checklist = [
        ("1 · Macro", "macro"),
        ("2 · Radar", "radar"),
        ("3 · Estructura de mercado", "estructura"),
        ("4 · Tesis", "tesis"),
    ]
    if not settings.public_mode:
        checklist.append(("5 · Ejecución", "ejecucion"))
    with st.sidebar:
        st.markdown("### Navegación")
        st.markdown("\n".join(f"- [{label}](#{anchor})" for label, anchor in checklist))
        st.caption("Niveles 1→4 en orden (sección 2): el macro manda sobre la tesis del activo.")
        st.divider()
        st.markdown(
            "**Extra**\n\n- [Próximos 7 días](#eventos)\n- [Validación de señales](#validacion)"
        )


def main() -> None:
    """Entry point for `streamlit run app/dashboard.py`."""
    st.set_page_config(page_title="Portafolio Crypto - Panel", layout="wide")
    # Streamlit Cloud: expose secrets (DATABASE_URL, PUBLIC_MODE, ...) as env vars so
    # core.config picks them up. No-op locally when there is no secrets file.
    try:
        for key, value in st.secrets.items():
            if isinstance(value, str):
                os.environ.setdefault(key, value)
    except Exception:  # noqa: BLE001 — st.secrets raises if no secrets configured
        pass
    settings = load_settings()
    conn = init_db(settings.db_path)
    try:
        _sidebar_nav(settings)
        st.title("Portafolio Crypto - Panel")
        st.caption(
            "Panel *pull*: los datos reflejan el último `run_ingest.py`. "
            "Frecuencia de consulta óptima: semanal (sección 2)."
        )
        _events_strip(conn, settings)
        st.divider()
        _section_macro(conn, settings)
        st.divider()
        _section_portfolio(conn, settings)
        st.divider()
        _section_market_structure(conn, settings)
        st.divider()
        _section_thesis(conn, settings)
        # Level 4 exposes the real Binance account — never on a public deploy.
        if settings.public_mode:
            st.divider()
            st.caption(
                "🔒 Vista pública: la sección de cartera real (nivel 4) está oculta. "
                "Ejecuta el panel localmente para el seguimiento de la cuenta."
            )
        else:
            st.divider()
            _section_execution(conn, settings)
        st.divider()
        _section_validation(settings)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

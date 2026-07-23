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

import html

import pandas as pd
import streamlit as st

from core.config import load_settings
from db.loader import init_db
from transform.indicators import (
    dca_status,
    macro_table,
    portfolio_table,
    thesis_tvl_table,
)

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


def _arrow_color(delta: float) -> tuple[str, str]:
    """Return (arrow, color) for a signed change: green up, red down, gray flat."""
    if delta > 0:
        return "▲", "#16a34a"  # green
    if delta < 0:
        return "▼", "#dc2626"  # red
    return "▬", "#9ca3af"  # neutral gray


def _delta_cell_html(record: dict) -> str:
    """Render the macro change cell: absolute (with unit) or percent, per series config.

    The hover tooltip reflects the effect *on crypto* (crypto_effect), not just the
    raw direction of the move.
    """
    if record.get("change_display") == "absolute":
        value = record.get("change_abs")
        text_fmt = f"{{:+,.0f}}{record.get('change_unit', '')}"
    else:
        value = record.get("change_pct")
        text_fmt = "{:+.2f}%"

    if value is None or pd.isna(value):
        return "<span style='opacity:0.45'>—</span>"

    arrow, color = _arrow_color(value)
    tip = _macro_effect_sentiment(value, record.get("crypto_effect"))
    return (
        f"<span title='{tip}' style='color:{color};font-weight:600'>"
        f"{arrow}&nbsp;{text_fmt.format(value)}</span>"
    )


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


def _change_span(x: float | None, sentiment_tooltip: bool = True) -> str:
    """Colored arrow + signed percent.

    When ``sentiment_tooltip`` is True the cell carries a bullish/bearish/neutral
    hover tooltip. It is disabled for Distance-to-ATH, where "below ATH" is a state,
    not a directional signal.
    """
    if x is None or pd.isna(x):
        return "<span style='opacity:0.45'>—</span>"
    arrow, color = _arrow_color(x)
    title = f"title='{_sentiment(x)}' " if sentiment_tooltip else ""
    return f"<span {title}style='color:{color};font-weight:600'>{arrow}&nbsp;{x:+.2f}%</span>"


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


def _macro_html(records: list[dict]) -> str:
    """Build the macro table as HTML: hover tooltips, colored deltas, date-only.

    An HTML table (rather than st.dataframe) is used so each indicator name can
    carry a per-row ``title`` tooltip and the delta cell can be individually
    colored — neither of which st.dataframe supports.
    """
    th = "padding:6px 16px 6px 0;text-align:{a};border-bottom:2px solid rgba(128,128,128,0.35);font-weight:600;"
    td = "padding:6px 16px 6px 0;text-align:{a};border-bottom:1px solid rgba(128,128,128,0.18);"
    num = "font-variant-numeric:tabular-nums;white-space:nowrap;"

    header = (
        "<tr>"
        f"<th style=\"{th.format(a='left')}\">Indicador</th>"
        f"<th style=\"{th.format(a='right')}\">Último</th>"
        f"<th style=\"{th.format(a='right')}\">Δ vs. previo</th>"
        f"<th style=\"{th.format(a='right')}\">Fecha ref.</th>"
        f"<th style=\"{th.format(a='right')}\">Publicado</th>"
        "</tr>"
    )
    body = ""
    for r in records:
        if r["value"] is None or pd.isna(r["value"]):
            continue
        desc = html.escape(str(r.get("description", "")), quote=True)
        label = html.escape(str(r["label"]))
        name = (
            f'<span title="{desc}" '
            'style="border-bottom:1px dotted currentColor;cursor:help">'
            f"{label}</span>"
        )
        body += (
            "<tr>"
            f"<td style=\"{td.format(a='left')}\">{name}</td>"
            f"<td style=\"{td.format(a='right')}{num}\">{_fmt_num(r['value'])}</td>"
            f"<td style=\"{td.format(a='right')}{num}\">{_delta_cell_html(r)}</td>"
            f"<td style=\"{td.format(a='right')}{num}\">{_fmt_date(r['ts'])}</td>"
            f"<td style=\"{td.format(a='right')}{num}\">{_fmt_date(r['ts_release'])}</td>"
            "</tr>"
        )
    return f'<table style="border-collapse:collapse;width:100%">{header}{body}</table>'


def _section_macro(conn, settings) -> None:
    """Level 1 — macro context. Rendered first and prominently (non-negotiable)."""
    st.header("1 · Macro — ¿hay apetito por riesgo?")
    df = macro_table(conn, settings)
    if df.empty or df["value"].notna().sum() == 0:
        st.info(
            "Sin datos macro. Configura `FRED_API_KEY` en `config/.env` y ejecuta "
            "`python run_ingest.py` para poblar CPI, PCE, NFP, Fed Funds, DXY y la curva."
        )
        return
    st.markdown(_macro_html(df.to_dict("records")), unsafe_allow_html=True)
    st.caption(
        "Δ = variación frente a la observación anterior (p. ej. mensual en CPI); NFP se "
        "muestra como empleos añadidos en miles (+K), no en %. Pasa el cursor sobre Δ para ver su "
        "efecto en cripto (alcista/bajista), y sobre el nombre del indicador para su definición. "
        "`Publicado` = fecha de primera publicación; los backtests filtran por ella para evitar "
        "look-ahead (§9)."
    )


def _portfolio_html(records: list[dict]) -> str:
    """Build the Radar table as HTML: logos, name tooltips, colored change cells."""
    th = "padding:6px 16px 6px 0;text-align:{a};border-bottom:2px solid rgba(128,128,128,0.35);font-weight:600;"
    td = "padding:6px 16px 6px 0;text-align:{a};border-bottom:1px solid rgba(128,128,128,0.18);"
    num = "font-variant-numeric:tabular-nums;white-space:nowrap;"
    cols = [
        ("Activo", "left"), ("Tramo", "left"), ("Precio", "right"),
        ("Cap. mercado", "right"), ("Vol. 24h", "right"),
        ("24h", "right"), ("7d", "right"), ("30d", "right"),
        ("Dist. ATH", "right"), ("Dilución", "right"),
    ]
    header = "<tr>" + "".join(f'<th style="{th.format(a=a)}">{c}</th>' for c, a in cols) + "</tr>"

    body = ""
    for r in records:
        if r["price"] is None or pd.isna(r["price"]):
            continue
        logo = ""
        if r.get("logo_url"):
            logo = (
                f'<img src="{html.escape(str(r["logo_url"]), quote=True)}" '
                'width="20" height="20" loading="lazy" '
                'style="vertical-align:middle;border-radius:50%;margin-right:6px">'
            )
        name = (
            f'{logo}<span title="{html.escape(str(r.get("description", "")), quote=True)}" '
            'style="border-bottom:1px dotted currentColor;cursor:help">'
            f'{html.escape(str(r["symbol"]))}</span>'
        )
        # Dilution cell: yellow warning glyph (left of value) + hover tooltip with
        # circulating share and next unlock. U+FE0E forces text presentation so the
        # CSS color applies (instead of the multicolor emoji).
        dil_val = _fmt_ratio(r["dilution_ratio"])
        if r.get("dilution_risk"):
            dil_val = f'<span style="color:#eab308;font-weight:700">⚠︎</span>&nbsp;{dil_val}'
        dil_cell = (
            f'<span title="{html.escape(_dilution_tooltip(r), quote=True)}" '
            f'style="cursor:help">{dil_val}</span>'
        )
        body += (
            "<tr>"
            f'<td style="{td.format(a="left")}">{name}</td>'
            f'<td style="{td.format(a="left")}">{html.escape(_fmt_tier(r["tier"]))}</td>'
            f'<td style="{td.format(a="right")}{num}">{_fmt_usd(r["price"])}</td>'
            f'<td style="{td.format(a="right")}{num}">{_fmt_big_usd(r["market_cap"])}</td>'
            f'<td style="{td.format(a="right")}{num}">{_fmt_big_usd(r["volume_24h"])}</td>'
            f'<td style="{td.format(a="right")}{num}">{_change_span(r["chg_24h"])}</td>'
            f'<td style="{td.format(a="right")}{num}">{_change_span(r["chg_7d"])}</td>'
            f'<td style="{td.format(a="right")}{num}">{_change_span(r["chg_30d"])}</td>'
            f'<td style="{td.format(a="right")}{num}">{_change_span(r["dist_ath"], sentiment_tooltip=False)}</td>'
            f'<td style="{td.format(a="right")}{num}">{dil_cell}</td>'
            "</tr>"
        )
    return f'<table style="border-collapse:collapse;width:100%">{header}{body}</table>'


def _section_portfolio(conn, settings) -> None:
    """Radar: price, changes, distance to ATH, dilution flag."""
    st.header("2 · Radar")
    df = portfolio_table(conn, settings)
    if df.empty or df["price"].notna().sum() == 0:
        st.info("Sin datos de precio. Ejecuta `python run_ingest.py`.")
        return

    dominance = df.attrs.get("btc_dominance")
    if dominance is not None:
        is_global = df.attrs.get("btc_dominance_is_global", False)
        label = "Dominancia BTC (mercado total)" if is_global else "Dominancia BTC (universo seguido)"
        st.metric(label, f"{dominance:.1f}%")

    st.markdown(_portfolio_html(df.to_dict("records")), unsafe_allow_html=True)
    st.caption(
        "Variaciones 24h/7d/30d muestran `—` hasta acumular historial de precios. "
        "Verde = alcista, rojo = bajista (pasa el cursor sobre 24h/7d/30d para ver la señal). "
        "El icono ⚠︎ amarillo marca circulante/máx por debajo del umbral; pasa el cursor sobre "
        "la dilución para ver el % circulante y el próximo unlock. Pasa el cursor sobre el "
        "activo para ver su tesis."
    )


def _section_thesis(conn, settings) -> None:
    """Level 3 — thesis health via TVL, grouped by thesis category."""
    st.header("3 · Tesis — TVL por categoría")
    df = thesis_tvl_table(conn, settings)
    if df.empty or df["tvl"].notna().sum() == 0:
        st.info("Sin datos de TVL. Ejecuta `python run_ingest.py`.")
        return
    show = pd.DataFrame(
        {
            "Activo": df["symbol"],
            "Categoría tesis": df["thesis_category"],
            "Tipo": df["kind"],
            "TVL": df["tvl"].map(_fmt_big_usd),
            "TVL 7d": df["tvl_chg_7d"].map(_fmt_pct),
            "TVL 30d": df["tvl_chg_30d"].map(_fmt_pct),
            "MC/TVL": df["mc_tvl"].map(_fmt_ratio),
        }
    )
    st.dataframe(show, hide_index=True, use_container_width=True)
    st.caption(
        "Agrupado por *categoría de tesis*, no por ticker, para exponer concentración "
        "disfrazada de diversificación (§5). MC/TVL alto = precio caro respecto a la "
        "actividad capturada."
    )


def _section_execution(conn, settings) -> None:
    """Level 4 — DCA plan state."""
    st.header("4 · Ejecución — plan DCA")
    status = dca_status(conn, settings)
    c1, c2, c3 = st.columns(3)
    c1.metric("Desplegado", _fmt_usd(status["deployed_usd"]))
    c2.metric("Planificado", _fmt_usd(status["planned_usd"]))
    c3.metric("Comisiones acum.", _fmt_usd(status["fees_usd"]))

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
    st.caption("Comisiones materiales con tickets de ~$17 — verificarlas antes de ejecutar (§2).")


def main() -> None:
    """Entry point for `streamlit run app/dashboard.py`."""
    st.set_page_config(page_title="cryptodash", layout="wide")
    settings = load_settings()
    conn = init_db(settings.db_path)
    try:
        st.title("cryptodash — panel cripto-macro")
        st.caption(
            "Panel *pull*: los datos reflejan el último `run_ingest.py`. "
            "Frecuencia de consulta óptima: semanal (§2)."
        )
        _section_macro(conn, settings)
        st.divider()
        _section_portfolio(conn, settings)
        st.divider()
        _section_thesis(conn, settings)
        st.divider()
        _section_execution(conn, settings)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

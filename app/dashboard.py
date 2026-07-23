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
    """Render the change cell: absolute (with unit) or percent, per series config."""
    if record.get("change_display") == "absolute":
        delta = record.get("change_abs")
        if delta is None or pd.isna(delta):
            return "<span style='opacity:0.45'>—</span>"
        arrow, color = _arrow_color(delta)
        unit = record.get("change_unit", "")
        return f"<span style='color:{color};font-weight:600'>{arrow}&nbsp;{delta:+,.0f}{unit}</span>"

    pct = record.get("change_pct")
    if pct is None or pd.isna(pct):
        return "<span style='opacity:0.45'>—</span>"
    arrow, color = _arrow_color(pct)
    return f"<span style='color:{color};font-weight:600'>{arrow}&nbsp;{pct:+.2f}%</span>"


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
        "muestra como empleos añadidos en miles (+K), no en %. `Publicado` = fecha de primera "
        "publicación; los backtests filtran por ella para evitar look-ahead (§9). Pasa el cursor "
        "sobre el nombre del indicador para ver su definición."
    )


def _section_portfolio(conn, settings) -> None:
    """Portfolio state: price, changes, distance to ATH, dilution flag."""
    st.header("2 · Estado de cartera")
    df = portfolio_table(conn, settings)
    if df.empty or df["price"].notna().sum() == 0:
        st.info("Sin datos de precio. Ejecuta `python run_ingest.py`.")
        return

    dominance = df.attrs.get("btc_dominance")
    if dominance is not None:
        is_global = df.attrs.get("btc_dominance_is_global", False)
        label = "Dominancia BTC (mercado total)" if is_global else "Dominancia BTC (universo seguido)"
        st.metric(label, f"{dominance:.1f}%")

    show = pd.DataFrame(
        {
            "Activo": df["symbol"],
            "Tramo": df["tier"],
            "Precio": df["price"].map(_fmt_usd),
            "24h": df["chg_24h"].map(_fmt_pct),
            "7d": df["chg_7d"].map(_fmt_pct),
            "30d": df["chg_30d"].map(_fmt_pct),
            "Dist. ATH": df["dist_ath"].map(_fmt_pct),
            "Dilución": df["dilution_ratio"].map(_fmt_ratio),
            "Riesgo dil.": df["dilution_risk"].map(lambda b: "⚠️" if b else ""),
        }
    )
    st.dataframe(show, hide_index=True, use_container_width=True)
    st.caption(
        "Las variaciones 24h/7d/30d muestran `—` hasta que se acumule suficiente "
        "historial de precios en la base de datos. ⚠️ = circulante/máx < umbral de dilución."
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

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
    st.header("MACRO")
    st.caption("¿Hay apetito por riesgo? — nivel 1 del checklist (§2).")
    df = macro_table(conn, settings)
    if df.empty or df["value"].notna().sum() == 0:
        st.info(
            "Sin datos macro. Configura `FRED_API_KEY` en `config/.env` y ejecuta "
            "`python run_ingest.py` para poblar CPI, PCE, NFP, Fed Funds, DXY y la curva."
        )
        return

    rows = []
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
    show = pd.DataFrame(rows)
    styler = show.style.map(_color_by_sign, subset=["Δ vs. previo"])
    st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        column_config={
            "Δ vs. previo": st.column_config.TextColumn(
                help="Variación frente a la observación anterior; NFP en miles de empleos (+K)."
            ),
            "Señal cripto": st.column_config.TextColumn(
                help="Efecto del movimiento en cripto (alcista/bajista), no la dirección cruda."
            ),
            "Publicado": st.column_config.TextColumn(
                help="Fecha de primera publicación; los backtests filtran por ella (look-ahead, §9)."
            ),
            "Descripción": st.column_config.TextColumn(
                width="medium", help="Definición del indicador."
            ),
        },
    )
    st.caption(
        "Tabla interactiva: arrastra las cabeceras para reordenar y usa el menú de cada "
        "columna para ordenar u ocultar. `Señal cripto` traduce el movimiento a su efecto "
        "en cripto (CPI/PCE/NFP/Fed Funds/DXY inversos; curva directa)."
    )


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
        "favorable a SOL/LINK (§2, nivel 2). Cambio en puntos porcentuales (pp)."
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

    st.header("2 · Radar")
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
            "Activo": st.column_config.TextColumn(help="Token seguido (§5)."),
            "Dist. ATH": st.column_config.TextColumn(help="Distancia al máximo histórico."),
            "Dilución": st.column_config.TextColumn(
                help="Circulante / suministro máximo. ⚠ = por debajo del umbral de dilución."
            ),
            "Próx. unlock": st.column_config.TextColumn(
                help="Próximo unlock del token (calendario en Fase 2)."
            ),
            "Tesis": st.column_config.TextColumn(
                width="medium", help="Tesis / métrica de invalidación (§5)."
            ),
        },
    )
    st.caption(
        "Tabla interactiva: arrastra para reordenar y usa el menú de columna para ordenar u "
        "ocultar. Verde = alcista, rojo = bajista en las variaciones; ⚠ en Dilución = "
        "circulante/máx por debajo del umbral. `Tesis` y `Próx. unlock` son ocultables. "
        "Las variaciones muestran `—` hasta acumular historial de precios."
    )


def _section_thesis(conn, settings) -> None:
    """Level 3 — thesis health. All tokens, grouped by category; interactive grid."""
    st.header("3 · Tesis — TVL por categoría")
    df = thesis_tvl_table(conn, settings)
    if df.empty:
        st.info("Sin activos configurados en `settings.yaml`.")
        return

    cat_notes = settings.raw.get("thesis_categories", {})
    rows = []
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
    show = pd.DataFrame(rows)
    styler = show.style.map(_color_by_sign, subset=["TVL 7d", "TVL 30d"])
    st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Categoría tesis": st.column_config.TextColumn(
                help="Agrupación por modo de fallo / megatendencia. Diversificar por "
                "categoría, no por ticker (§5). Ver la columna 'Nota categoría'."
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
                "capturada (value accrual, §2)."
            ),
            "Tesis": st.column_config.TextColumn(
                width="medium", help="Tesis / métrica de invalidación (§5)."
            ),
            "Nota categoría": st.column_config.TextColumn(
                width="medium", help="Explicación breve de la categoría de tesis."
            ),
        },
    )
    st.caption(
        "Todos los activos, agrupados por *categoría de tesis* para exponer concentración "
        "disfrazada de diversificación (§5). La tabla interactiva no admite tooltips por "
        "celda: las explicaciones por valor van en las columnas ocultables `Tesis` y "
        "`Nota categoría`, y en la ⓘ de cada cabecera. Verde/rojo = variación de TVL."
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
    st.set_page_config(page_title="Portafolio Crypto - Panel", layout="wide")
    settings = load_settings()
    conn = init_db(settings.db_path)
    try:
        st.title("Portafolio Crypto - Panel")
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

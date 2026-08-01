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

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from core.config import load_settings  # noqa: E402
from db.loader import init_db  # noqa: E402
from db.queries import series_history, upcoming_events  # noqa: E402
from transform.indicators import (  # noqa: E402
    behavioral_scorecard,
    btc_onchain_summary,
    capital_deployed_summary,
    coinbase_premium_summary,
    dca_allocator,
    dca_status,
    dca_vs_baseline_table,
    drift_vs_target,
    drift_vs_target_asset,
    dvol_summary,
    earn_rewards_summary,
    execution_summary,
    fear_greed_summary,
    fed_liquidity_summary,
    fed_net_liquidity_series,
    holdings_by_group,
    holdings_table,
    liquidations_summary,
    macro_table,
    monthly_contribution_advice,
    portfolio_table,
    realized_pnl_fifo,
    regime_scoreboard,
    rotation_summary,
    source_discrepancy_table,
    stablecoins_summary,
    thesis_invalidation_table,
    thesis_tvl_table,
    value_accrual_table,
    wallet_pnl_history,
    wallet_pnl_table,
    wallet_value_history,
)
from validation.backtest import funding_zscore_backtest, signal_battery  # noqa: E402
from transform.portfolio_risk import correlation_matrix, portfolio_risk_summary  # noqa: E402
from transform.rally_quality import (  # noqa: E402
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


def _fmt_pct0(x: float | None) -> str:
    """Format a percent with no sign and no decimals ('42%'), or an em dash."""
    return "—" if x is None or pd.isna(x) else f"{x:.0f}%"


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


_REGIME_STYLE = {
    "risk_on": ("🟢", "Risk-on"),
    "neutral": ("🟠", "Neutral"),
    "risk_off": ("🔴", "Risk-off"),
}


def _regime_vote_label(vote: int) -> str:
    """Vote -> arrow label for the regime breakdown table."""
    return {1: "▲ risk-on", 0: "▬ neutral", -1: "▼ risk-off"}.get(vote, "—")


def _color_regime_vote(cell: object) -> str:
    """Styler CSS: green risk-on, red risk-off, gray neutral."""
    s = str(cell)
    if "risk-on" in s:
        return "color:#16a34a;font-weight:600"
    if "risk-off" in s:
        return "color:#dc2626;font-weight:600"
    return "color:#9ca3af"


def _section_regime(conn, settings) -> None:
    """B1 — semáforo de régimen: el *marcador rector* que agrega niveles 1+2 (sección 2).

    Un estado risk-on / neutral / risk-off, transparente (desglose por señal), que
    operacionaliza la regla dura: si 1-2 están en rojo, no se compra aunque la tesis esté
    perfecta. Es un estado **semanal**, no un gatillo — y **bloquea** malas decisiones en
    rojo en vez de invitar a operar.
    """
    r = regime_scoreboard(conn, settings)
    if not r.get("has_data"):
        return
    st.header("Régimen de mercado", anchor="regimen")
    icon, text = _REGIME_STYLE[r["label"]]
    c1, c2 = st.columns([1, 3])
    c1.metric(
        "Régimen (niveles 1+2)",
        f"{icon} {text}",
        delta=f"score {r['score']:+d} · {r['n']} señales",
        delta_color="off",
        help="Marcador rector: suma de votos de las señales macro y de estructura. Estado "
        "semanal, no un gatillo de operación (sección 2).",
    )
    if r["label"] == "risk_off":
        c2.error(
            "**Regla dura (sección 2):** niveles 1-2 en rojo → **no comprar** aunque la tesis "
            "del activo sea perfecta. Considera **posponer** el aporte mensual unos días."
        )
    elif r["label"] == "neutral":
        c2.info(
            "Régimen **mixto**. Ejecuta el plan DCA con cautela; revisa el desglose y los "
            "eventos próximos antes de desplegar el aporte."
        )
    else:
        c2.success(
            "**Viento de cola risk-on.** Contexto favorable para el plan DCA según lo escrito "
            "(sección 2, nivel 4). No es una señal de compra: sigue tu plan."
        )
    with st.expander("Desglose del régimen (transparente)"):
        rows = [
            {
                "Señal": c["name"],
                "Lectura": c["reading"],
                "Voto": _regime_vote_label(c["vote"]),
                "Por qué": c["rationale"],
            }
            for c in r["components"]
        ]
        show = pd.DataFrame(rows)
        st.dataframe(
            show.style.map(_color_regime_vote, subset=["Voto"]),
            hide_index=True,
            width="stretch",
            column_config={"Por qué": st.column_config.TextColumn(width="large")},
        )
        st.caption(
            f"Score = suma de votos (+1 risk-on / 0 neutral / −1 risk-off). Umbrales fijos y "
            f"pocos componentes por diseño (anti-overfitting, sección 9): score ≥ {r['risk_on_at']} "
            f"= risk-on, ≤ {r['risk_off_at']} = risk-off. No se optimizan pesos sobre el histórico."
        )


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
        "(inflación, empleo, tipos, dólar y condiciones financieras —NFCI, spread HY, real "
        "yield, VIX— inversos; curva directa)."
    )
    sel = _selected_row(event)
    if sel is not None:
        series_id, label = refs[sel]
        _drilldown_chart(conn, "fred", series_id, label)

    _liquidity_sentiment_block(conn, settings)


def _liquidity_sentiment_block(conn, settings) -> None:
    """Level 1 add-ons: Fed net liquidity (A1), stablecoin dry powder (A7), Fear & Greed (A6).

    Net liquidity is the single macro driver crypto tracks most; stablecoin supply is the
    "dry powder" waiting to enter; Fear & Greed is a *contrarian weekly* overlay — shown as
    a quiet caption, never highlighted, by design (sección 2: diseñar contra el over-trading).
    """
    liq = fed_liquidity_summary(conn, settings)
    stab = stablecoins_summary(conn, settings)
    fng = fear_greed_summary(conn, settings)
    if not (liq.get("has_data") or stab.get("has_data") or fng.get("has_data")):
        return

    st.markdown("**Liquidez y sentimiento**")
    c1, c2 = st.columns(2)
    if liq.get("has_data"):
        chg = liq.get("chg_13w_pct")
        c1.metric(
            "Liquidez neta de la Fed",
            f"${liq['latest'] / 1000:,.2f} T",
            delta=None if chg is None else f"{chg:+.1f}% · 13 sem",
            help="WALCL − TGA − RRP (FRED, miles de millones→billones USD). El driver macro "
            "que cripto sigue más de cerca: subiendo = viento de cola risk-on; bajando = "
            "risk-off. Útil para decidir si ejecutar el aporte mensual (nivel 1).",
        )
    if stab.get("has_data"):
        chg = stab.get("chg_30d_pct")
        c2.metric(
            "Stablecoins (dry powder)",
            _fmt_big_usd(stab["latest"]),
            delta=None if chg is None else f"{chg:+.1f}% · 30 d",
            help="Capitalización agregada de stablecoins (DefiLlama). Capital aparcado listo "
            "para entrar; creciendo = liquidez entrando al ecosistema (combustible, nivel 2).",
        )
    if liq.get("has_data"):
        series = fed_net_liquidity_series(conn, settings)
        if not series.empty:
            with st.expander("Ver histórico de liquidez neta"):
                st.line_chart(series.rename("USD (miles de millones)"))

    if fng.get("has_data"):
        chg = fng.get("chg_7d")
        chg_txt = "" if chg is None else f" ({chg:+.0f} vs. 7 d)"
        st.caption(
            f"**Fear & Greed:** {fng['value']:.0f}/100 — {fng['label']}{chg_txt}. Sentimiento "
            "agregado (Alternative.me); úsalo como contexto semanal y **contrarian**, no como "
            "gatillo de operación (sección 2)."
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


def _upcoming_unlocks(
    settings, within_days: int = 30
) -> list[tuple[str, str, int, float | None]]:
    """Return [(symbol, date, days_until, pct)] for configured next_unlock within the window.

    Reads config/assets_meta.yaml (manual, sección 5). ``pct`` = % del circulante que se
    desbloquea (``unlock_pct``, B5) — la magnitud, no solo la fecha; None si no está puesta.
    Empty until dates are filled in.
    """
    today = datetime.now(timezone.utc).date()
    out: list[tuple[str, str, int, float | None]] = []
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
            pct = meta.get("unlock_pct") if isinstance(meta, dict) else None
            out.append((symbol, str(raw), days, pct if isinstance(pct, (int, float)) else None))
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
    for sym, date, days, pct in _upcoming_unlocks(settings, within_days=7):
        note = "desbloqueo de tokens" + (f" · {pct:.0f}% circ." if pct is not None else "")
        items.append((days, date, ":material/lock_open:", f"Unlock {sym}", note))

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


def _rotation_sentiment_block(conn, settings) -> None:
    """Level 2 add-ons: rotation (A9 ETH/BTC, TOTAL2/3), Coinbase premium (A8), DVOL (A5).

    Rotation answers "¿entorno favorable a altcoins?" (complementa la dominancia BTC);
    Coinbase premium reads unleveraged US spot conviction; DVOL is the options market's
    implied volatility ("VIX de cripto"). Weekly context, no intraday (sección 2).
    """
    rot = rotation_summary(conn, settings)
    prem = coinbase_premium_summary(conn, settings)
    dvol = dvol_summary(conn, settings)
    if not (rot.get("has_data") or prem.get("has_data") or dvol.get("has_data")):
        return

    st.markdown("**Rotación y sentimiento de mercado**")
    if rot.get("has_data"):
        eb, t2, t3 = rot["eth_btc"], rot["total2"], rot["total3"]
        c = st.columns(3)
        c[0].metric(
            "ETH/BTC",
            _fmt_ratio(eb["latest"]),
            delta=None if eb["chg_30d_pct"] is None else f"{eb['chg_30d_pct']:+.1f}% · 30 d",
            help="Ratio ETH/BTC: barómetro clásico de rotación core→alt. Subiendo = apetito "
            "por altcoins, entorno favorable a SOL/LINK (sección 2 nivel 2).",
        )
        c[1].metric(
            "TOTAL2 (mcap ex-BTC)",
            _fmt_big_usd(t2["latest"]),
            delta=None if t2["chg_30d_pct"] is None else f"{t2['chg_30d_pct']:+.1f}% · 30 d",
            help="Capitalización total de cripto excluyendo BTC: el ciclo alt sin el ruido de BTC.",
        )
        c[2].metric(
            "TOTAL3 (ex-BTC/ETH)",
            _fmt_big_usd(t3["latest"]),
            delta=None if t3["chg_30d_pct"] is None else f"{t3['chg_30d_pct']:+.1f}% · 30 d",
            help="Total excluyendo BTC y ETH: el resto del mercado altcoin.",
        )

    bits = [f"{p['asset']} {p['premium_pct']:+.2f}%" for p in prem.get("per_asset", [])]
    if bits:
        st.caption(
            "**Coinbase premium:** " + " · ".join(bits) + ". Positivo = demanda spot US "
            "(ETF/tesorerías compran en Coinbase); convicción real, no apalancada (nivel 2)."
        )
    dbits = []
    for d in dvol.get("per_currency", []):
        chg = "" if d["chg_30d"] is None else f" ({d['chg_30d']:+.1f} vs. 30 d)"
        dbits.append(f"{d['currency']} {d['latest']:.1f}{chg}")
    if dbits:
        st.caption(
            "**DVOL (vol. implícita, 'VIX de cripto'):** " + " · ".join(dbits) + ". Alta = miedo "
            "(a veces contrarian alcista); baja = complacencia (Deribit, opciones)."
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
                "Basis": _fmt_pct(r.get("basis_pct")),
                "L/S retail": _fmt_ratio(r.get("long_short")),
                "L/S top": _fmt_ratio(r.get("top_ls_account")),
                "Taker": _fmt_ratio(r.get("taker_ratio")),
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
            .map(_color_by_sign, subset=["Basis", "Precio 7d", "OI 7d", "OI 30d"])
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
                "Basis": st.column_config.TextColumn(
                    help="Prima perp−spot (%). Positiva y creciente = largos apalancados pagando "
                    "prima (frágil); negativa = pesimismo/backwardation. Instantánea (perp sin "
                    "vencimiento; el funding es el carry)."
                ),
                "L/S retail": st.column_config.TextColumn(
                    help="Ratio long/short de cuentas retail (Binance). >1 = mayoría en largos."
                ),
                "L/S top": st.column_config.TextColumn(
                    help="Ratio long/short de las cuentas TOP ('smart money'). La divergencia "
                    "retail>>top (retail muy largo, top plano/corto) suele marcar techo local."
                ),
                "Taker": st.column_config.TextColumn(
                    help="Ratio de volumen agresor comprador/vendedor. >1 = presión compradora; "
                    "extremos = agotamiento del flujo."
                ),
                "OI 7d": st.column_config.TextColumn(help="Variación del interés abierto (USD) a 7 días."),
                "OI 30d": st.column_config.TextColumn(
                    help="Variación del interés abierto (USD) a 30 días (~límite del historial de OI)."
                ),
            },
        )
        st.caption(
            "Rally por divergencia precio/OI + funding z (sección 2 nivel 2, sección 8). "
            "**Basis** = prima perp−spot (apalancamiento); **L/S retail vs. top** = "
            "posicionamiento minorista vs. smart money; **Taker** = flujo agresor."
        )
    else:
        st.info("Sin datos de derivados. Ejecuta `python run_ingest.py` (extra `.[markets]`).")

    _rotation_sentiment_block(conn, settings)

    liq = liquidations_summary(conn, settings)
    if liq.get("has_liq"):
        st.caption(
            f"**Liquidaciones (último día):** longs {_fmt_big_usd(liq['long_usd'])} · "
            f"shorts {_fmt_big_usd(liq['short_usd'])}. Más longs liquidados = capitulación / "
            "riesgo de cascada; más shorts = combustible de short-squeeze (nivel 2). "
            "Requiere el daemon `run_liquidations.py`."
        )

    # Upcoming unlocks (manual config, sección 5: revisar siempre).
    unlocks = _upcoming_unlocks(settings, within_days=30)
    if unlocks:
        st.write(
            "**Próximos unlocks (≤30 d):** "
            + " · ".join(
                f"{sym} {date} ({days} d{f', {pct:.0f}% circ.' if pct is not None else ''})"
                for sym, date, days, pct in unlocks
            )
        )
        st.caption(
            "La **magnitud** (`unlock_pct`, % del circulante) manda tanto como la fecha: un unlock "
            "grande y próximo se marca en **rojo** en el tablero de invalidación (sección 5)."
        )
    else:
        st.caption(
            "Próximos unlocks: ninguno configurado. Añade `next_unlock: \"YYYY-MM-DD\"` y "
            "`unlock_pct: <%>` por token en `config/assets_meta.yaml` (sección 5: revisar siempre)."
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
    _btc_onchain_block(conn, settings)


# A10: friendly labels for the Blockchain.com chart slugs.
_BCOM_CHART_LABELS = {
    "hash-rate": "Hash rate",
    "difficulty": "Dificultad",
    "n-transactions": "Transacciones/día",
    "n-unique-addresses": "Direcciones activas",
    "miners-revenue": "Ingresos de mineros",
}


def _btc_onchain_block(conn, settings) -> None:
    """A10: BTC network fundamentals (hash rate, tx count, active addresses...) — level 3.

    A partial, free substitute for paid on-chain metrics (§4): network security and real
    usage back BTC's thesis. Not MVRV/SOPR (those stay out of scope).
    """
    summary = btc_onchain_summary(conn, settings)
    if not summary.get("has_data"):
        return
    st.markdown("**BTC on-chain (fundamental de red)**")
    per = summary["per_chart"]
    cols = st.columns(min(len(per), 5) or 1)
    for col, item in zip(cols, per):
        label = _BCOM_CHART_LABELS.get(item["chart"], item["chart"])
        chg = item.get("chg_30d_pct")
        col.metric(
            label,
            _fmt_big_usd(item["latest"]) if item["latest"] >= 1e6 else _fmt_num(item["latest"]),
            delta=None if chg is None else f"{chg:+.1f}% · 30 d",
            delta_color="off",
        )
    st.caption(
        "Fundamentales de red de BTC (Blockchain.com, gratis). Seguridad (hash rate/dificultad) "
        "y uso real (transacciones/direcciones) sostienen la tesis (nivel 3). Sustituto **parcial** "
        "de métricas on-chain de pago; no incluye MVRV/SOPR (fuera de alcance, sección 4)."
    )


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
                "Peso sin efectivo": holdings["weight_ex_cash_pct"].map(
                    lambda w: "—" if w is None or pd.isna(w) else f"{w:.1f}%"
                ),
                "Nota": holdings["note"],
            }
        )
        st.dataframe(
            show,
            hide_index=True,
            width="stretch",
            column_config={
                "Logo": st.column_config.ImageColumn("", width="small"),
                "Peso": st.column_config.TextColumn(
                    help="Peso sobre el **valor total** de la cartera (efectivo incluido)."
                ),
                "Peso sin efectivo": st.column_config.TextColumn(
                    help="Peso sobre el **capital invertido** (excluye stablecoins/efectivo): cuánto "
                    "pesa el token frente a los demás tokens, ignorando la caja. El efectivo muestra —."
                ),
            },
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

    # --- Drift vs. objetivo (B4) ---------------------------------------------
    _drift_view(conn, settings, holdings)

    # --- Riesgo de cartera (B2/B3) -------------------------------------------
    _risk_view(conn, settings, holdings)

    # --- Scorecard conductual (B6) -------------------------------------------
    _scorecard_view(conn, settings)

    # --- ¿Despliego el aporte de este mes? (B7) ------------------------------
    _contribution_advice_view(conn, settings, holdings)

    # --- Lista de compra del mes (Parte G — asignador del DCA) ----------------
    _dca_shopping_list_view(conn, settings, holdings)

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


def _drift_view(conn, settings, holdings) -> None:
    """B4: allocation drift by tier vs. §5 targets + rebalance-by-contribution hint (level 4)."""
    df = drift_vs_target(conn, settings, holdings)
    if df.empty:
        return
    st.subheader("Drift vs. objetivo (asignación por tramo)")
    band = settings.raw.get("portfolio", {}).get("drift_band_pp", 5)
    rows = [
        {
            "Tramo": r["tier_label"],
            "Actual": f"{r['current_pct']:.1f}%",
            "Objetivo": f"{r['target_pct']:.0f}%",
            "Drift": f"{r['drift_pp']:+.1f} pp{' ⚠' if r['over_band'] else ''}",
            "Valor": _fmt_usd(r["value_usd"]),
        }
        for r in df.to_dict("records")
    ]
    show = pd.DataFrame(rows)
    st.dataframe(
        show.style.map(_color_dilution, subset=["Drift"]),
        hide_index=True,
        width="stretch",
        column_config={
            "Drift": st.column_config.TextColumn(
                help=f"Actual − objetivo (pp). ⚠ = fuera de la banda de ±{band} pp."
            )
        },
    )
    most = df.attrs.get("most_underweight")
    cash = df.attrs.get("cash_usd", 0.0)
    if most:
        label = df.loc[df["tier"] == most, "tier_label"].iloc[0]
        st.caption(
            "**Rebalanceo por aportación:** el aporte de este mes va al tramo más infra-ponderado "
            f"→ **{label}**. Rebalancea **añadiendo** al tramo bajo, no vendiendo (evita comisiones "
            f"e impuestos, sección 4). Efectivo disponible: {_fmt_usd(cash)}. Los activos fuera de "
            "la sección 5 (p. ej. WBETH) aparecen como *Sin tramo*."
        )


def _corr_heatmap(cm: pd.DataFrame):
    """Altair heatmap of a correlation matrix (B2). None if the matrix is empty."""
    if cm.empty:
        return None
    syms = list(cm.columns)
    records = [
        {"x": a, "y": b, "corr": (None if pd.isna(cm.loc[a, b]) else round(float(cm.loc[a, b]), 2))}
        for a in syms
        for b in syms
    ]
    data = alt.Data(values=records)
    base = alt.Chart(data).encode(
        x=alt.X("x:N", title=None, sort=syms),
        y=alt.Y("y:N", title=None, sort=syms),
    )
    heat = base.mark_rect().encode(
        color=alt.Color(
            "corr:Q",
            scale=alt.Scale(scheme="redblue", domain=[-1, 1], reverse=True),
            legend=alt.Legend(title="ρ"),
        )
    )
    text = base.mark_text(baseline="middle").encode(
        text=alt.Text("corr:Q", format=".2f"),
        color=alt.condition("abs(datum.corr) > 0.6", alt.value("white"), alt.value("black")),
    )
    return (heat + text).properties(height=min(70 + 26 * len(syms), 420))


def _risk_view(conn, settings, holdings) -> None:
    """B2/B3: portfolio risk — vol, beta, HHI, drawdown, risk contribution + correlation (level 4)."""
    summary = portfolio_risk_summary(conn, settings, holdings)
    if not summary.get("has_data"):
        return
    st.subheader("Riesgo de cartera")
    hhi = summary.get("hhi")
    c = st.columns(5)
    c[0].metric(
        "Vol. anualizada", _fmt_pct0(summary.get("port_vol_annual_pct")),
        help="Volatilidad anualizada de la cartera (√(wᵀΣw), 365 d). Sobre precios propios.",
    )
    c[1].metric(
        "Beta a BTC", _fmt_ratio(summary.get("port_beta_btc")),
        help="Sensibilidad de la cartera a BTC (ponderada). >1 amplifica a BTC, <1 atenúa.",
    )
    eff = summary.get("effective_n")
    c[2].metric(
        "N efectivo", "—" if not eff else f"{eff:.1f}",
        help=f"1/HHI (HHI={hhi:.2f} si aplica). Nº efectivo de posiciones: bajo = concentrado.",
    )
    c[3].metric(
        "Max drawdown", _fmt_pct(summary.get("max_drawdown_pct")),
        help="Peor caída pico-valle de la cartera simulada (tenencias actuales a precios pasados).",
    )
    c[4].metric(
        "Correlación media", _fmt_ratio(summary.get("avg_correlation")),
        help="Correlación media entre posiciones. Alta = diversificación aparente, no real (§5).",
    )

    rows = [
        {
            "Activo": p["symbol"],
            "Peso": f"{p['weight_pct']:.1f}%",
            "Vol. anual": _fmt_pct0(p["vol_annual_pct"]),
            "Beta BTC": _fmt_ratio(p["beta_btc"]),
            "Contrib. riesgo": f"{p['risk_contribution_pct']:.1f}%",
        }
        for p in summary["per_asset"]
    ]
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        column_config={
            "Contrib. riesgo": st.column_config.TextColumn(
                help="Contribución a la varianza de la cartera (RCᵢ = wᵢ·(Σw)ᵢ/(wᵀΣw)). Una "
                "posición del 15% del capital puede ser el 40% del riesgo si es la más volátil."
            ),
            "Beta BTC": st.column_config.TextColumn(help="Beta del activo frente a BTC."),
        },
    )

    ch = _corr_heatmap(correlation_matrix(conn, settings, holdings))
    if ch is not None:
        st.altair_chart(ch, width="stretch")
    pairs = summary.get("high_corr_pairs", [])
    hi = settings.raw.get("indicators", {}).get("risk", {}).get("high_correlation", 0.8)
    if pairs:
        txt = " · ".join(f"{a}–{b} {c:+.2f}" for a, b, c in pairs)
        st.caption(
            f"**Pares muy correlacionados (|ρ|≥{hi:.1f}):** {txt}. Cuentan casi como una sola "
            "apuesta — su tamaño combinado debe respetar el límite del tramo (§5, concentración "
            "disfrazada de diversificación)."
        )
    st.caption(
        f"Riesgo sobre precios propios (backfill); ventana de {summary['window_days']} d. "
        "Diversificación **real** = por modo de fallo, no por número de tickers (sección 5)."
    )


def _drift_bar(records: list[dict], settings):
    """Altair horizontal bar of per-asset drift (pp); under-weight (negative) in red."""
    if not records:
        return None
    data = alt.Data(values=records)
    return (
        alt.Chart(data)
        .mark_bar()
        .encode(
            x=alt.X("drift:Q", title="Drift vs. objetivo (pp)"),
            y=alt.Y("asset:N", title=None, sort="-x"),
            color=alt.condition(alt.datum.drift < 0, alt.value("#dc2626"), alt.value("#16a34a")),
            tooltip=[alt.Tooltip("asset:N", title="Activo"), alt.Tooltip("drift:Q", title="Drift (pp)")],
        )
        .properties(height=min(40 + 24 * len(records), 320))
    )


def _dca_shopping_list_view(conn, settings, holdings) -> None:
    """Parte G (G5-d/G5.3): the month's DCA shopping list from the allocator (level 4).

    Interactive: two sliders (cash to keep as reserve, cash to deposit now) drive the deployable
    budget — the list and the *projected* wallet weights react to both. Still pull-not-push (§2):
    it recomputes on demand, no live tickers.
    """
    st.markdown("**Lista de compra del mes**")

    # Only the new deposit is allocated; existing cash is left untouched (cash_keep = cash_now).
    drift0 = drift_vs_target_asset(conn, settings, holdings)
    if drift0.empty:
        return
    cash_now = float(
        next((r["value_usd"] for r in drift0.to_dict("records") if r["asset"] == "CASH"), 0.0)
    )
    default_deposit = float(settings.raw.get("dca", {}).get("monthly_contribution_min_usd", 200))

    deposit = st.slider(
        "Efectivo a depositar ahora",
        min_value=0.0,
        max_value=2000.0,
        value=default_deposit,
        step=25.0,
        format="$%.0f",
        key="dca_deposit",
        help="Dinero nuevo que aportas en este momento. Solo este importe se reparte entre los "
        "activos infra-ponderados; tu caja actual no se toca.",
    )

    a = dca_allocator(conn, settings, holdings=holdings, deposit_usd=deposit, cash_keep_usd=cash_now)
    if not a.get("has_data"):
        return

    deployable = a.get("deployable_usd", 0.0)
    st.caption(
        f"Se reparte tu depósito de **{_fmt_usd(deposit)}** entre los activos infra-ponderados "
        "(tu caja actual no se toca)."
    )
    rows = [
        {
            "Logo": settings.meta_for(i["asset"]).get("logo_url") or "",
            "Activo": i["asset"],
            "Actual": f"{i['current_pct']:.1f}%",
            "Objetivo": f"{i['target_pct']:.1f}%",
            "Drift": _fmt_pp(i["drift_pp"]) if i["drift_pp"] is not None else "—",
            "Veto": "🚫" if i["vetoed"] else "",
            "Ticket": _fmt_usd(i["usd"]),
        }
        for i in a["items"]
    ]
    show = pd.DataFrame(rows)
    st.dataframe(
        show.style.map(_color_by_sign, subset=["Drift"]),
        hide_index=True,
        width="stretch",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Ticket": st.column_config.TextColumn(
                help="Cuánto de tu aporte va a este activo: rebalanceo por bandas hacia el objetivo "
                "(compra al infra-ponderado, por construcción). Los sobre-ponderados reciben $0."
            ),
            "Veto": st.column_config.TextColumn(
                help="🚫 = tesis invalidada (tablero de invalidación en rojo): excluido del reparto, "
                "su parte se redistribuye (evita promediar a la baja sobre una trampa de valor)."
            ),
        },
    )
    st.caption(
        f"Total repartido: **{_fmt_usd(a['total_allocated_usd'])}** de {_fmt_usd(deployable)} "
        "depositados. **Asignación por bandas de rebalanceo, no predicción de precio** (Parte G): el "
        "depósito va al más infra-ponderado por drift. Te dice *dónde va el aporte*, no *cuándo "
        "comprar*; ejecución **manual** (la key de Binance es de solo lectura). Capa 4 (tilt) "
        "desactivada hasta validar."
    )
    bars = _drift_bar(
        [
            {"asset": i["asset"], "drift": round(i["drift_pp"], 1) if i["drift_pp"] is not None else 0.0}
            for i in a["items"]
        ],
        settings,
    )
    if bars is not None:
        st.altair_chart(bars, width="stretch")


_REGIME_ICON = {"risk_on": "🟢 risk-on", "neutral": "🟠 neutral", "risk_off": "🔴 risk-off"}


def _contribution_advice_view(conn, settings, holdings) -> None:
    """B7: '¿despliego el aporte de este mes?' — compone régimen + eventos + drift (§2 Q4, level 4)."""
    a = monthly_contribution_advice(conn, settings, holdings=holdings)
    if not a.get("has_data"):
        return
    st.subheader("¿Despliego el aporte de este mes?")
    minimum = _fmt_usd(a["minimum_usd"]) if a.get("minimum_usd") else "el aporte"
    if a["recommendation"] == "execute":
        tier = a.get("target_tier_label")
        dest = f" → tramo **{tier}** (el más infra-ponderado)" if tier else ""
        st.success(
            f"✅ **Ejecutar** el aporte (mín. {minimum}){dest}. Rebalancea **añadiendo** al tramo "
            "bajo. No es una señal de compra: es tu plan escrito (sección 2, nivel 4)."
        )
    else:
        n = a.get("postpone_days")
        when = f" ~{n} día(s)" if n else " hasta que mejore el régimen"
        reasons = ("Motivos: " + "; ".join(a["reasons"]) + ".") if a.get("reasons") else ""
        st.warning(f"⏸️ **Posponer**{when}. {reasons}")

    ctx = []
    if a.get("regime_label"):
        ctx.append(f"régimen {_REGIME_ICON.get(a['regime_label'], a['regime_label'])} (score {a['regime_score']:+d})")
    if a.get("blocking_events"):
        ctx.append(f"{len(a['blocking_events'])} evento(s) de alto impacto ≤{7} d")
    if a.get("target_tier_label"):
        ctx.append(f"tramo objetivo: {a['target_tier_label']}")
    if ctx:
        st.caption(
            " · ".join(ctx) + ". Compone el semáforo de régimen (niveles 1-2), el calendario de "
            "eventos y el drift de asignación (nivel 4) en una sola decisión mensual."
        )


def _scorecard_view(conn, settings) -> None:
    """B6: behavioral scorecard — did your activity beat buy-and-hold? (§2 thesis, level 4)."""
    s = behavioral_scorecard(conn, settings)
    if not s.get("has_data"):
        return
    st.subheader("Scorecard conductual — ¿tu actividad batió a mantener?")
    c = st.columns(4)
    c[0].metric(
        "Tu retorno (operado)", _fmt_pct(s.get("actual_ret_pct")),
        help="Retorno del capital que desplegaste vía operaciones (valor actual / neto invertido − 1).",
    )
    c[1].metric(
        "Hold BTC (mismo capital)", _fmt_pct(s.get("bh_btc_ret_pct")),
        help="Contrafactual: si cada dólar operado hubiera ido a BTC en la fecha de cada operación "
        "y se mantuviera hasta hoy.",
    )
    edge = s.get("edge_vs_hold_btc_pp")
    c[2].metric(
        "Edge vs. hold BTC", _fmt_pp(edge), delta_color="off",
        help="Tu retorno − hold BTC (pp). >0 = elegir/temporizar batió a solo mantener BTC.",
    )
    c[3].metric(
        "Coste de fricción", _fmt_pct0(s.get("fees_drag_pct")),
        help="Comisiones acumuladas como % del capital invertido — el lastre de operar (sección 1/4).",
    )

    bits = []
    if s.get("n_trades") is not None:
        freq = f", {s['trades_per_month']:.1f}/mes" if s.get("trades_per_month") else ""
        bits.append(f"{s['n_trades']} operaciones ({s['n_buys']} compras / {s['n_sells']} ventas{freq})")
    if s.get("weighted_timing_edge_pp") is not None:
        bits.append(f"edge de *timing* vs. DCA ciego: {s['weighted_timing_edge_pp']:+.1f} pp (ponderado)")
    if s.get("fees_usd") is not None:
        bits.append(f"comisiones {_fmt_usd(s['fees_usd'])}")
    if bits:
        st.caption(" · ".join(bits) + ".")

    if edge is not None:
        if edge > 0:
            st.success(f"✅ Tu selección/timing batió a mantener BTC por **{edge:+.1f} pp**.")
        else:
            st.warning(
                f"⚠️ Mantener BTC habría rendido **{-edge:.1f} pp más**. La tesis anti-over-trading "
                "(sección 2): con horizonte de trimestres, operar menos suele ganar."
            )
    if s.get("bh_uncovered"):
        st.caption(
            f"Nota: {s['bh_uncovered']} operación(es) anteriores a la ventana de 365 días de "
            "CoinGecko gratis quedan fuera (no hay precio BTC en su fecha); el scorecard cubre "
            f"las {s['n_trades']} operaciones con histórico."
        )
    st.caption(
        "Compara **el mismo flujo de capital** (tus operaciones) en tus activos vs. todo en BTC. "
        "Mide el capital que operaste, no lo adquirido vía Earn/Convert (honesto)."
    )


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
    realized = realized_pnl_fifo(conn, settings)
    rp = realized.attrs.get("total_realized_pnl_usd", 0.0)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Valor (sin efectivo)", _fmt_usd(tv))
    c2.metric("Coste (posiciones con base)", _fmt_usd(tc))
    c3.metric("PnL no realizado", _fmt_signed_usd(tp), delta=f"{tp / tc * 100:+.1f}%" if tc else None)
    c4.metric(
        "PnL realizado (FIFO)", _fmt_signed_usd(rp),
        help="Ganancia/pérdida ya materializada en ventas, emparejando cada venta con los lotes de "
        "compra más antiguos (FIFO). Bruto de comisiones. Cero si aún no has vendido (D2).",
    )

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

    cap = capital_deployed_summary(conn, settings)
    if cap.get("has_flows"):
        total_val = holdings.attrs.get("total_value_usd", 0.0)  # incl. efectivo
        net = cap["net_deployed_usd"]
        roi = (total_val / net - 1) * 100 if net else None
        k1, k2, k3 = st.columns(3)
        k1.metric(
            "Capital neto aportado", _fmt_usd(net),
            help="Depósitos − retiros (fiat orders + PSE/pagos + P2P), o el valor manual "
            "`net_deployed_usd` si está configurado.",
        )
        k2.metric("Valor total (incl. efectivo)", _fmt_usd(total_val))
        k3.metric(
            "Retorno sobre aportes", "—" if roi is None else f"{roi:+.1f}%",
            help="Valor total de la cuenta ÷ capital neto aportado − 1. La rentabilidad real "
            "sobre lo que has ingresado (secciones 1, 2).",
        )
        if cap.get("is_manual"):
            st.caption("Capital neto **manual** (`net_deployed_usd` en config).")
        else:
            st.caption(
                "Capital auto (fiat orders + PSE + P2P). Si falta algún canal, fija el total real "
                "en `net_deployed_usd` (config/settings.local.yaml) — es autoritativo."
                + (f" · {cap['unconverted']} movimiento(s) sin tasa FX excluidos." if cap["unconverted"] else "")
            )

    earn = earn_rewards_summary(conn, settings)
    if earn.get("has_rewards"):
        detail = " · ".join(
            f"{r['asset']} {_fmt_amount(r['amount'])}" for r in earn["per_asset"][:6]
        )
        st.caption(
            f"**Rendimiento Earn (ventana):** {_fmt_usd(earn['total_usd'])} en recompensas de "
            f"Simple Earn — {detail}. Ingreso pasivo que suma al retorno del nivel 4."
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
    if not realized.empty:
        parts = [
            f"{r['symbol']} {_fmt_signed_usd(r['realized_pnl_usd'])}"
            for r in realized.to_dict("records")
        ]
        unmatched = [r["symbol"] for r in realized.to_dict("records") if r["unmatched_qty"] > 1e-9]
        note = (
            f" ({', '.join(unmatched)} con ventas sin lote de compra en `trades`)" if unmatched else ""
        )
        st.caption("**PnL realizado (FIFO) por activo:** " + " · ".join(parts) + note + ".")

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


@st.cache_data(ttl=3600, show_spinner="Validando batería de señales (C1)…")
def _battery_result(db_path_str: str, horizon: int) -> dict:
    """C1 signal battery + FDR (sección 8/9). Cached ~1h. Own connection → serializable dict."""
    settings = load_settings()
    conn = init_db(Path(db_path_str))
    try:
        return signal_battery(conn, settings, horizon=horizon)
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

    _battery_view(settings)
    _source_discrepancy_view(settings)


def _battery_view(settings) -> None:
    """C1: signal battery + Benjamini-Hochberg FDR — several level-1/2 signals at once (sección 8/9)."""
    st.subheader("Batería de señales + corrección FDR (C1)")
    res = _battery_result(str(settings.db_path), 30)
    sigs = res.get("signals", [])
    if not sigs:
        st.info("Sin historial suficiente para la batería todavía.")
        return

    def _verdict(s: dict) -> str:
        if s["status"] != "ok":
            return "sin datos"
        return "✓ significativa" if s.get("significant") else "no significativa"

    show = pd.DataFrame(
        {
            "Señal": [s["name"] for s in sigs],
            "Hipótesis": [s["hypothesis"] for s in sigs],
            "n": [s["n_signal"] for s in sigs],
            "Edge (pp)": ["—" if s["edge"] is None else f"{s['edge']:+.2f}" for s in sigs],
            "p": ["—" if s["pvalue"] is None else f"{s['pvalue']:.3f}" for s in sigs],
            "q (FDR)": ["—" if s["qvalue"] is None else f"{s['qvalue']:.3f}" for s in sigs],
            "Veredicto": [_verdict(s) for s in sigs],
        }
    )
    st.dataframe(
        show.style.map(_color_by_sign, subset=["Edge (pp)"]),
        hide_index=True,
        width="stretch",
        column_config={
            "n": st.column_config.TextColumn(
                help="Nº de episodios **no solapados** (fechas de señal separadas ≥ horizonte). "
                "No son días sueltos: colapsar rachas evita contar la misma tendencia mil veces."
            ),
            "Edge (pp)": st.column_config.TextColumn(help="Retorno medio de la señal − baseline, a 30 d."),
            "p": st.column_config.TextColumn(help="p-valor por permutación (bootstrap), dos colas."),
            "q (FDR)": st.column_config.TextColumn(
                help="p-valor ajustado por Benjamini-Hochberg sobre las señales con datos. "
                "Significativa si q ≤ 0.10 (controla la tasa de falsos descubrimientos)."
            ),
        },
    )
    n_sig = sum(1 for s in sigs if s.get("significant"))
    tested = res.get("n_tested", 0)
    if n_sig == 0:
        verdict = (
            f"**Ninguna de las {tested} señales con datos supera el umbral FDR (q ≤ "
            f"{res.get('fdr_alpha', 0.10):.2f}).** No es un fracaso: es el resultado honesto que "
            "justifica quedarse con la regla más simple (sección 8). La versión ingenua por día marcaba "
            "'liquidez neta' como significativa (+5.8 pp), pero era **autocorrelación** — al colapsar "
            "las rachas en episodios independientes el edge se desvanece."
        )
    else:
        verdict = (
            f"**{n_sig} de {tested} señales** superan el umbral FDR (q ≤ {res.get('fdr_alpha', 0.10):.2f}). "
            "Aun así, ~1.2 ciclos de historia → indicativo, no ley."
        )
    st.caption(
        "Horizonte 30 d, precios **Binance** (sección 9, no CoinGecko), macro *point-in-time* por "
        "fecha de publicación (`ts_release`). " + verdict + " Cacheado 1 h."
    )


def _source_discrepancy_view(settings) -> None:
    """C4: same asset, different free sources → the spread (§9 'don't mix sources')."""
    conn = init_db(settings.db_path)
    try:
        df = source_discrepancy_table(conn, settings)
    finally:
        conn.close()
    if df.empty:
        return
    st.subheader("Discrepancia entre fuentes (C4)")

    def _pct(x) -> str:
        return "—" if x is None or pd.isna(x) else f"{x:+.3f}%"

    show = pd.DataFrame(
        {
            "Logo": [settings.meta_for(s).get("logo_url") or "" for s in df["symbol"]],
            "Activo": df["symbol"],
            "CoinGecko": df["coingecko"].map(_fmt_usd),
            "Binance": df["binance"].map(_fmt_usd),
            "Coinbase": df["coinbase"].map(_fmt_usd),
            "Δ Binance": df["binance_vs_cg_pct"].map(_pct),
            "Δ Coinbase": df["coinbase_vs_cg_pct"].map(_pct),
            "Spread": df["spread_pct"].map(lambda x: "—" if pd.isna(x) else f"{x:.3f}%"),
        }
    )
    st.dataframe(
        show,
        hide_index=True,
        width="stretch",
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Δ Binance": st.column_config.TextColumn(help="Binance vs. CoinGecko (referencia)."),
            "Δ Coinbase": st.column_config.TextColumn(help="Coinbase vs. CoinGecko (referencia)."),
            "Spread": st.column_config.TextColumn(help="(máx − mín) / mín entre las fuentes."),
        },
    )
    st.caption(
        "El *mismo* activo cotiza distinto en cada fuente (spread de venue + USDT vs USD + desfase de "
        "snapshot). El *market cap* = precio × supply hereda esta brecha. Por eso **sección 9 prohíbe "
        "mezclar fuentes en una misma serie**: el asignador usa Binance, el panel CoinGecko, nunca "
        "cruzados. Parte del spread es desfase de captura (CoinGecko es snapshot; los cierres son EOD)."
    )


def _sidebar_nav(settings) -> None:
    """Anchor navigation to jump between sections.

    Links follow the mandated level 1->4 order (sección 2): Macro stays first so the hierarchy
    (macro overrides thesis) is always visible. The level-4 link is hidden in public mode,
    where that section is not rendered.
    """
    checklist = [
        ("Régimen de mercado", "regimen"),
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
        _section_regime(conn, settings)
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

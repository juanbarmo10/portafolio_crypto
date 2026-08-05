"""Colombian tax layer: lots, PEPS disposals and COP/USD PnL (RESEARCH.md §17 Paso 6).

Builds **immutable acquisition lots** from the real ``trades``, freezing each cost in COP at
the acquisition-day TRM (Art. 269 E.T.), and matches sells against the oldest lots (PEPS/FIFO)
into disposals with a **regime split**: a lot held ≥ ``long_term_months`` (24) at disposal is
*ganancia ocasional* (15%), otherwise *renta ordinaria* (progressive). The headline product is
:func:`cop_usd_pnl_table` — the same position shown in **both** currencies, because a loss in
USD can be a **taxable gain in COP** when the peso devalues between buy and sell.

**Estimates, not filings** (RESEARCH.md §17): every figure is ``ESTIMADO — verificar con contador``.
Personal data: never synced to a shared/cloud DB, hidden under ``PUBLIC_MODE``.

Pure/deterministic given the DB; no network. Prices use CoinGecko latest (the panel's source);
the TRM is the ``banrep`` series. Crypto→crypto Converts are a known blind spot (they are dropped
upstream in ``parse_convert`` — RESEARCH.md §17); only what reaches ``trades`` is lotted here.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from dateutil.relativedelta import relativedelta

from core.config import Settings
from db.queries import latest_observation, series_history
from transform.indicators import STABLECOINS


def _trm_series(conn: sqlite3.Connection, settings: Settings) -> pd.Series:
    """The TRM COP/USD history as an ascending Series (empty if not ingested)."""
    trm_cfg = settings.raw.get("fiscal", {}).get("trm", {})
    source = trm_cfg.get("source", "banrep")
    series_id = trm_cfg.get("series_id", "TRM:COP_USD")
    return series_history(conn, source, series_id).dropna()


def _trm_asof(trm: pd.Series, ts: pd.Timestamp) -> float | None:
    """Last TRM at or before ``ts`` (as-of; the rate is carried over weekends/holidays)."""
    if trm.empty:
        return None
    prior = trm.loc[trm.index <= ts]
    return float(prior.iloc[-1]) if not prior.empty else None


def _fee_cop(fee: float | None, fee_ccy: str | None, price: float | None,
             base: str, trm: float | None) -> float | None:
    """Fee converted to COP (deductible from cost). USD/stable → face value; base asset →
    fee×price; anything else (e.g. BNB) → None (can't convert without its price)."""
    if fee is None or trm is None or fee == 0:
        return 0.0 if fee == 0 else None
    ccy = (fee_ccy or "").upper()
    if ccy in STABLECOINS or ccy in {"USD", "USDT", "USDC"}:
        fee_usd = float(fee)
    elif ccy == base and price:
        fee_usd = float(fee) * float(price)
    else:
        return None
    return fee_usd * trm


def build_tax_lots(conn: sqlite3.Connection, settings: Settings) -> dict[str, list[dict[str, Any]]]:
    """Replay ``trades`` chronologically into lots, disposals and PEPS consumption.

    Buys create immutable lots (COP cost frozen at the acquisition-day TRM, ``matures_at`` =
    acquired + ``long_term_months``). Sells consume the **oldest** lots first (PEPS); each
    consumption row carries the prorated frozen cost, the COP gain, and the regime
    (*ganancia_ocasional* if the lot had matured at disposal, else *renta_ordinaria*).
    Stablecoin bases are skipped (no crypto lot). Returns ``{lots, disposals, consumption}``
    as lists of dict rows ready for the loaders; ``lots`` carry live ``units_remaining``.
    """
    fiscal = settings.raw.get("fiscal", {})
    months = int(fiscal.get("long_term_months", 24))
    trm = _trm_series(conn, settings)

    rows = conn.execute(
        "SELECT trade_id, symbol, side, ts, price, amount, cost, fee, fee_currency "
        "FROM trades ORDER BY ts"
    ).fetchall()

    open_lots: dict[str, deque] = {}
    lots: list[dict[str, Any]] = []
    disposals: list[dict[str, Any]] = []
    consumption: list[dict[str, Any]] = []

    for trade_id, symbol, side, ts, price, amount, cost, fee, fee_ccy in rows:
        base = symbol.split("/")[0]
        if base in STABLECOINS or amount is None:
            continue
        t = pd.Timestamp(ts)
        trm_t = _trm_asof(trm, t)
        cost_usd = (
            float(cost) if cost is not None
            else (float(amount) * float(price) if price is not None else None)
        )

        if side == "buy":
            cost_cop = cost_usd * trm_t if (cost_usd is not None and trm_t) else None
            matures = (t.to_pydatetime() + relativedelta(months=months))
            lot = {
                "lot_id": str(trade_id), "asset": base, "acquired_at": t.isoformat(),
                "units": float(amount), "units_remaining": float(amount),
                "cost_usd": cost_usd, "trm_acquisition": trm_t, "cost_cop": cost_cop,
                "fees_cop": _fee_cop(fee, fee_ccy, price, base, trm_t),
                "matures_at": pd.Timestamp(matures).isoformat(), "origin": "compra",
                "source_ref": str(trade_id),
            }
            lots.append(lot)
            open_lots.setdefault(base, deque()).append(lot)

        elif side == "sell":
            proceeds_usd = cost_usd
            proceeds_cop = proceeds_usd * trm_t if (proceeds_usd is not None and trm_t) else None
            unit_proceeds_cop = (
                proceeds_cop / float(amount) if (proceeds_cop is not None and amount) else None
            )
            disp = {
                "disposal_id": str(trade_id), "asset": base, "disposed_at": t.isoformat(),
                "units": float(amount), "proceeds_usd": proceeds_usd, "trm_disposal": trm_t,
                "proceeds_cop": proceeds_cop, "kind": "venta", "unmatched": 0.0,
            }
            disposals.append(disp)
            remaining = float(amount)
            queue = open_lots.setdefault(base, deque())
            while remaining > 1e-12 and queue:
                lot = queue[0]
                take = min(lot["units_remaining"], remaining)
                frac = take / lot["units"] if lot["units"] else 0.0
                cost_cop_portion = lot["cost_cop"] * frac if lot["cost_cop"] is not None else None
                proceeds_portion = unit_proceeds_cop * take if unit_proceeds_cop is not None else None
                gain_cop = (
                    proceeds_portion - cost_cop_portion
                    if (proceeds_portion is not None and cost_cop_portion is not None) else None
                )
                matured = pd.Timestamp(lot["matures_at"]) <= t
                consumption.append({
                    "disposal_id": str(trade_id), "lot_id": lot["lot_id"], "units": take,
                    "cost_cop": cost_cop_portion, "gain_cop": gain_cop,
                    "regime": "ganancia_ocasional" if matured else "renta_ordinaria",
                })
                lot["units_remaining"] -= take
                remaining -= take
                if lot["units_remaining"] <= 1e-12:
                    queue.popleft()
            # Units sold beyond any cost lot (e.g. coins from Earn/Convert not in `trades`): their
            # proceeds have NO frozen cost, so their gain is NOT computed. Recorded, never dropped
            # silently (RESEARCH.md §17 — errors here are high and silent). Surfaced in disposal_summary.
            disp["unmatched"] = remaining if remaining > 1e-12 else 0.0

    return {"lots": lots, "disposals": disposals, "consumption": consumption}


def _current_price_usd(conn: sqlite3.Connection, settings: Settings, asset: str) -> float | None:
    """Latest CoinGecko USD price for a symbol (the panel's price source)."""
    cid = next((a["coingecko_id"] for a in settings.assets if a["symbol"] == asset), None)
    if not cid:
        return None
    obs = latest_observation(conn, "coingecko", f"{cid}:price")
    return None if obs is None else float(obs[1])


def cop_usd_pnl_table(
    conn: sqlite3.Connection, settings: Settings, built: dict | None = None
) -> pd.DataFrame:
    """Unrealized PnL per asset in **USD and COP side by side** (RESEARCH.md §17).

    Cost is the frozen COP cost of the lots still open (Art. 269); current value is the live
    CoinGecko price × the latest TRM. The point: ``pnl_usd`` and ``pnl_cop`` can disagree in
    sign — a USD loss becomes a taxable COP gain if the peso devalued. Rows only for assets
    with open lots and a current price. Attrs: ``trm_now``, ``patrimonio_cop`` (Σ frozen cost —
    the Art. 74/271 fiscal net worth, at COST not market).

    Columns: [asset, units, cost_usd, cost_cop, value_usd, value_cop, pnl_usd, pnl_cop].
    """
    built = built if built is not None else build_tax_lots(conn, settings)
    trm = _trm_series(conn, settings)
    trm_now = float(trm.iloc[-1]) if not trm.empty else None

    agg: dict[str, dict[str, float]] = {}
    for lot in built["lots"]:
        rem = lot["units_remaining"]
        if rem <= 1e-12 or lot["units"] <= 0:
            continue
        frac = rem / lot["units"]
        a = agg.setdefault(lot["asset"], {"units": 0.0, "cost_usd": 0.0, "cost_cop": 0.0})
        a["units"] += rem
        if lot["cost_usd"] is not None:
            a["cost_usd"] += lot["cost_usd"] * frac
        if lot["cost_cop"] is not None:
            a["cost_cop"] += lot["cost_cop"] * frac

    rows: list[dict[str, Any]] = []
    for asset, a in agg.items():
        price = _current_price_usd(conn, settings, asset)
        if price is None or trm_now is None:
            continue
        value_usd = a["units"] * price
        value_cop = value_usd * trm_now
        rows.append({
            "asset": asset, "units": a["units"],
            "cost_usd": a["cost_usd"], "cost_cop": a["cost_cop"],
            "value_usd": value_usd, "value_cop": value_cop,
            "pnl_usd": value_usd - a["cost_usd"], "pnl_cop": value_cop - a["cost_cop"],
        })
    df = pd.DataFrame(
        rows,
        columns=["asset", "units", "cost_usd", "cost_cop", "value_usd", "value_cop", "pnl_usd", "pnl_cop"],
    )
    if not df.empty:
        df = df.sort_values("value_usd", ascending=False).reset_index(drop=True)
    df.attrs["trm_now"] = trm_now
    df.attrs["patrimonio_cop"] = float(sum(a["cost_cop"] for a in agg.values()))
    return df


def pnl_attribution_cop(
    conn: sqlite3.Connection, settings: Settings, built: dict | None = None
) -> dict[str, Any]:
    """Split the unrealized **COP** PnL into a crypto (USD price) part and an FX (TRM) part.

    A user living in pesos but holding only USD-denominated assets is implicitly **long USD/COP**
    for the whole portfolio — a position never chosen and invisible in every price-based risk view
    (INVERSOR_IDEAS §2.1/§4.2). The decomposition is exact (crypto valued at the average buy TRM,
    FX as the rate move on the current position — the convention the review reports):

        trm_avg    =  Σ cost_cop / Σ cost_usd                (implied average acquisition TRM)
        crypto_cop = (Σ value_usd − Σ cost_usd) · trm_avg    (USD price move, at your buy rate)
        fx_cop     =  Σ value_usd · (TRM_now − trm_avg)       (rate move on the current position)
        total_cop  = crypto_cop + fx_cop = Σ value_cop − Σ cost_cop = pnl_cop   ✔ reconciles

    Returns {has_data, crypto_cop, fx_cop, total_cop, pnl_usd, trm_now, trm_cost_avg}. ESTIMADO.
    """
    df = cop_usd_pnl_table(conn, settings, built=built)
    trm_now = df.attrs.get("trm_now")
    if df.empty or trm_now is None:
        return {"has_data": False}
    cost_usd = float(df["cost_usd"].sum())
    value_usd = float(df["value_usd"].sum())
    cost_cop = float(df["cost_cop"].sum())
    if cost_usd <= 0:
        return {"has_data": False}
    trm_avg = cost_cop / cost_usd  # implied average acquisition TRM (Σ frozen cost / Σ USD cost)
    crypto_cop = (value_usd - cost_usd) * trm_avg
    fx_cop = value_usd * (trm_now - trm_avg)
    return {
        "has_data": True,
        "crypto_cop": crypto_cop,
        "fx_cop": fx_cop,
        "total_cop": crypto_cop + fx_cop,
        "pnl_usd": value_usd - cost_usd,
        "trm_now": trm_now,
        "trm_cost_avg": trm_avg,
    }


# Note: tax lots/disposals/consumption are computed on demand from `trades` (build_tax_lots)
# and consumed in memory by the views — they are NOT persisted (the tables had no reader).


# --- Fase B: maturation metrics + pre-sale PEPS simulator (RESEARCH.md §17/§6.3) --------------


def maturity_summary(
    conn: sqlite3.Connection, settings: Settings, built: dict | None = None
) -> pd.DataFrame:
    """Per-asset maturation, measured in **units** (not value) — RESEARCH.md §17.

    A lot matures ``long_term_months`` (24) after acquisition; once matured, selling it is
    *ganancia ocasional* (15%) instead of *renta ordinaria* (progressive, up to 39%). Reports
    open units, matured units, % matured, and the next lot's maturity date/days — the timing
    that ``lot_maturity_soon`` will alert on. Columns: [asset, units_open, units_matured,
    pct_matured, cost_cop_open, next_maturity, days_to_next].
    """
    built = built if built is not None else build_tax_lots(conn, settings)
    today = pd.Timestamp(datetime.now(timezone.utc))
    agg: dict[str, dict[str, Any]] = {}
    for lot in built["lots"]:
        rem = lot["units_remaining"]
        if rem <= 1e-12:
            continue
        a = agg.setdefault(
            lot["asset"], {"open": 0.0, "matured": 0.0, "cost_cop": 0.0, "next": None}
        )
        a["open"] += rem
        matures = pd.Timestamp(lot["matures_at"])
        if matures <= today:
            a["matured"] += rem
        elif a["next"] is None or matures < a["next"]:
            a["next"] = matures
        if lot["cost_cop"] is not None and lot["units"]:
            a["cost_cop"] += lot["cost_cop"] * (rem / lot["units"])

    rows = []
    for asset, a in agg.items():
        nxt = a["next"]
        rows.append({
            "asset": asset,
            "units_open": a["open"],
            "units_matured": a["matured"],
            "pct_matured": (a["matured"] / a["open"] * 100.0) if a["open"] else 0.0,
            "cost_cop_open": a["cost_cop"],
            "next_maturity": None if nxt is None else nxt.isoformat(),
            "days_to_next": None if nxt is None else int((nxt - today).days),
        })
    df = pd.DataFrame(
        rows,
        columns=["asset", "units_open", "units_matured", "pct_matured", "cost_cop_open",
                 "next_maturity", "days_to_next"],
    )
    return df.sort_values("cost_cop_open", ascending=False).reset_index(drop=True) if not df.empty else df


# --- Renta ordinaria (<24m): tabla progresiva Art. 241 E.T. (cédula general) ------------------
# Base en UVT: (desde, tarifa marginal, impuesto acumulado en UVT al inicio del rango). Es LEY
# (Art. 241 tras Ley 2277/2022), no un umbral ajustable; se deja override en `fiscal.art241_brackets`
# por si la ley cambia. La renta ordinaria de cripto <24m se suma a esta cédula.
_ART241_BRACKETS = [
    (0.0, 0.00, 0.0),
    (1090.0, 0.19, 0.0),
    (1700.0, 0.28, 116.0),
    (4100.0, 0.33, 788.0),
    (8670.0, 0.35, 2296.0),
    (18970.0, 0.37, 5901.0),
    (31000.0, 0.39, 10352.0),
]
# UVT 2025 (Resolución DIAN 000193/2024 = $49.799). ACTUALIZAR al valor oficial del año fiscal
# (2026: pendiente de la Resolución DIAN — verificar con el contador). Override: `fiscal.uvt_cop`.
_DEFAULT_UVT_COP = 49799.0


def _resolve_uvt(uvt_cfg: Any) -> float:
    """Resolve ``fiscal.uvt_cop`` to the current tax year's UVT (COP). Accepts a scalar, a
    ``{year: value}`` map (uses this year, else the latest), or None (→ 2025 default)."""
    if isinstance(uvt_cfg, dict) and uvt_cfg:
        year = datetime.now(timezone.utc).year
        return float(uvt_cfg.get(year) or uvt_cfg.get(str(year)) or uvt_cfg[max(uvt_cfg)])
    return float(uvt_cfg) if uvt_cfg else _DEFAULT_UVT_COP


def art241_tax_cop(base_cop: float, uvt_cop: float, brackets: list | None = None) -> float:
    """Impuesto de renta progresivo (Art. 241 E.T., cédula general) en COP para una base en COP.

    La base se pasa a UVT, se ubica el rango y ``impuesto = (base_uvt − desde)·tarifa + acumulado``,
    y se devuelve a COP. Cero bajo el piso de 1.090 UVT. **ESTIMADO** — el contador liquida.
    """
    if base_cop <= 0 or uvt_cop <= 0:
        return 0.0
    table = brackets or _ART241_BRACKETS
    base_uvt = base_cop / uvt_cop
    lo, rate, acc = table[0]
    for frm, r, a in table:
        if base_uvt >= frm:
            lo, rate, acc = frm, r, a
        else:
            break
    return ((base_uvt - lo) * rate + acc) * uvt_cop


def estimate_ordinary_tax(settings: Settings, gain_cop: float) -> dict[str, Any]:
    """Impuesto de renta ordinaria sobre una ganancia <24m, por su **impacto marginal** en la
    cédula general (RESEARCH.md §17): ``impuesto = Art241(ingreso + ganancia) − Art241(ingreso)``.

    La ganancia <24m **se apila** sobre tus demás ingresos anuales, así que su tarifa depende de ese
    total — no es un % plano. Lee ``fiscal.annual_ordinary_income_cop`` (tus otros ingresos de la
    cédula general) y ``fiscal.uvt_cop`` (UVT del año; por defecto la de 2025). ``has_config`` False
    si no fijaste el ingreso. Devuelve {has_config, income_cop, uvt_cop, income_uvt, gain_cop, tax_cop,
    marginal_rate_pct, effective_rate_pct}. **ESTIMADO**.
    """
    fiscal = settings.raw.get("fiscal", {})
    income = fiscal.get("annual_ordinary_income_cop")
    uvt = _resolve_uvt(fiscal.get("uvt_cop"))
    override = fiscal.get("art241_brackets")
    table = (
        [(float(b["from_uvt"]), float(b["rate"]), float(b["plus_uvt"])) for b in override]
        if override else None
    )
    if income is None:
        return {"has_config": False, "uvt_cop": uvt, "gain_cop": gain_cop}
    income = float(income)
    g = max(float(gain_cop), 0.0)
    tax = art241_tax_cop(income + g, uvt, table) - art241_tax_cop(income, uvt, table)
    base_uvt = (income + g) / uvt
    marginal = 0.0
    for frm, r, _a in (table or _ART241_BRACKETS):
        if base_uvt >= frm:
            marginal = r
    return {
        "has_config": True,
        "income_cop": income,
        "uvt_cop": uvt,
        "income_uvt": income / uvt,
        "gain_cop": g,
        "tax_cop": max(tax, 0.0),
        "marginal_rate_pct": marginal * 100.0,
        "effective_rate_pct": (max(tax, 0.0) / g * 100.0) if g > 0 else 0.0,
    }


def simulate_peps_sale(
    conn: sqlite3.Connection, settings: Settings, asset: str, units: float,
    price_usd: float | None = None, built: dict | None = None,
) -> dict[str, Any]:
    """The most valuable function (RESEARCH.md §17): "if I sell N units of X", **before** selling.

    Consumes the oldest open lots (PEPS) at today's price and TRM, and splits the result into
    **ganancia ocasional** (lots matured ≥24 months → 15%) vs **renta ordinaria** (< 24 months →
    your estimated marginal), in COP and USD side by side. Tax is estimated only on positive
    gains per regime; the ordinary tax is ``None`` until you set a real marginal rate (it stays
    an ESTIMATE either way — RESEARCH.md §17/§12). ``shortfall`` = units beyond your lotted holdings
    (acquired via Earn/Convert, no cost basis in ``trades``). ``has_data`` False if no price/TRM.

    Returns a dict with proceeds/cost/gain (COP+USD), the per-regime split, tax estimates and
    the list of consumed lots.
    """
    fiscal = settings.raw.get("fiscal", {})
    occ_rate = float(fiscal.get("occasional_gain_rate_pct", 15.0)) / 100.0
    ord_rate = float(fiscal.get("ordinary_marginal_rate_pct", 0.0)) / 100.0
    ord_is_estimate = bool(fiscal.get("ordinary_rate_is_estimate", True))

    built = built if built is not None else build_tax_lots(conn, settings)
    lots = sorted(
        (lot for lot in built["lots"] if lot["asset"] == asset and lot["units_remaining"] > 1e-12),
        key=lambda lot: lot["acquired_at"],
    )
    open_units = sum(lot["units_remaining"] for lot in lots)
    trm = _trm_series(conn, settings)
    trm_now = float(trm.iloc[-1]) if not trm.empty else None
    price = price_usd if price_usd is not None else _current_price_usd(conn, settings, asset)
    if price is None or trm_now is None or units <= 0:
        return {"has_data": False, "asset": asset, "open_units": open_units}

    unit_proceeds_usd = float(price)
    unit_proceeds_cop = float(price) * trm_now
    today = pd.Timestamp(datetime.now(timezone.utc))
    sold_units = min(units, open_units)
    shortfall = max(0.0, units - open_units)

    consumed: list[dict[str, Any]] = []
    occ_cop = occ_usd = ord_cop = ord_usd = 0.0
    cost_cop_tot = cost_usd_tot = 0.0
    remaining = sold_units
    for lot in lots:
        if remaining <= 1e-12:
            break
        take = min(lot["units_remaining"], remaining)
        frac = take / lot["units"] if lot["units"] else 0.0
        cost_cop_p = (lot["cost_cop"] or 0.0) * frac
        cost_usd_p = (lot["cost_usd"] or 0.0) * frac
        gain_cop = unit_proceeds_cop * take - cost_cop_p
        gain_usd = unit_proceeds_usd * take - cost_usd_p
        matured = pd.Timestamp(lot["matures_at"]) <= today
        consumed.append({
            "lot_id": lot["lot_id"], "acquired_at": lot["acquired_at"], "units": take,
            "cost_cop": cost_cop_p, "gain_cop": gain_cop, "gain_usd": gain_usd,
            "regime": "ganancia_ocasional" if matured else "renta_ordinaria",
        })
        if matured:
            occ_cop += gain_cop
            occ_usd += gain_usd
        else:
            ord_cop += gain_cop
            ord_usd += gain_usd
        cost_cop_tot += cost_cop_p
        cost_usd_tot += cost_usd_p
        remaining -= take

    tax_occ_cop = max(0.0, occ_cop) * occ_rate
    # Ordinary tax: PROGRESSIVE (Art. 241, marginal impact) when income+UVT are configured; else
    # the flat `ordinary_marginal_rate_pct` fallback (None if unset). RESEARCH.md §17.
    ord_est = estimate_ordinary_tax(settings, ord_cop)
    if ord_est["has_config"]:
        tax_ord_cop = ord_est["tax_cop"]
        ord_rate = ord_est["marginal_rate_pct"] / 100.0
    else:
        tax_ord_cop = (max(0.0, ord_cop) * ord_rate) if ord_rate > 0 else None
    return {
        "has_data": True,
        "asset": asset,
        "units_requested": float(units),
        "sold_units": sold_units,
        "shortfall": shortfall,
        "open_units": open_units,
        "price_usd": unit_proceeds_usd,
        "trm_now": trm_now,
        "proceeds_usd": unit_proceeds_usd * sold_units,
        "proceeds_cop": unit_proceeds_cop * sold_units,
        "cost_usd": cost_usd_tot,
        "cost_cop": cost_cop_tot,
        "gain_usd": occ_usd + ord_usd,
        "gain_cop": occ_cop + ord_cop,
        "occasional_gain_cop": occ_cop,
        "occasional_gain_usd": occ_usd,
        "ordinary_gain_cop": ord_cop,
        "ordinary_gain_usd": ord_usd,
        "occ_rate_pct": occ_rate * 100.0,
        "ord_rate_pct": ord_rate * 100.0,
        "ord_is_estimate": ord_is_estimate,
        "ordinary_progressive": ord_est["has_config"],
        "tax_occasional_cop": tax_occ_cop,
        "tax_ordinary_cop": tax_ord_cop,
        "tax_total_cop": tax_occ_cop + (tax_ord_cop or 0.0),
        "consumed": consumed,
    }


def disposal_summary(
    conn: sqlite3.Connection, settings: Settings, built: dict | None = None
) -> dict[str, Any]:
    """Count this year's disposals and flag habitual-activity risk (RESEARCH.md §17).

    Sells **and** swaps (permutas) count; buys do not. If the DIAN sees a habitual trading
    pattern, the crypto becomes **inventory (movable asset)** and the 24-month rule stops
    applying entirely — so this counter is a prominent guardrail. ``habitual_risk`` trips at
    ``fiscal.habituality.warn_disposals_per_year``. Also surfaces permutas (each a taxable
    event that resets the maturity clock, §7.2) and the realized COP gain of the year.

    ``unmatched_events`` lists disposals that sold more units than were lotted (proceeds with no
    cost basis — gain not computed; RESEARCH.md §17/§2.3).

    Returns {year, count, ventas, permutas, threshold, habitual_risk, permuta_events,
    realized_gain_cop, unmatched_events}.
    """
    built = built if built is not None else build_tax_lots(conn, settings)
    hab = settings.raw.get("fiscal", {}).get("habituality", {})
    threshold = int(hab.get("warn_disposals_per_year", 4))
    year = datetime.now(timezone.utc).year

    disposals = built["disposals"]
    this_year = [d for d in disposals if str(d["disposed_at"])[:4] == str(year)]
    ventas = [d for d in this_year if d.get("kind") == "venta"]
    permutas = [d for d in this_year if d.get("kind") == "permuta"]
    unmatched = [
        {"asset": d["asset"], "date": str(d["disposed_at"])[:10], "units": d["unmatched"]}
        for d in disposals if d.get("unmatched", 0.0) > 1e-9
    ]

    disp_date = {d["disposal_id"]: d["disposed_at"] for d in disposals}
    realized = sum(
        c["gain_cop"] for c in built["consumption"]
        if c["gain_cop"] is not None and str(disp_date.get(c["disposal_id"], ""))[:4] == str(year)
    )

    return {
        "year": year,
        "count": len(this_year),
        "ventas": len(ventas),
        "permutas": len(permutas),
        "threshold": threshold,
        "habitual_risk": len(this_year) >= threshold,
        "unmatched_events": unmatched,
        "permuta_events": permutas,
        "realized_gain_cop": realized,
    }


def short_term_disposals(
    conn: sqlite3.Connection, settings: Settings, built: dict | None = None
) -> dict[str, Any]:
    """This-year disposals that fell into **renta ordinaria** (held < ``long_term_months``).

    The behavioral mirror (INVERSOR_IDEAS §2.1): a coin sold before the 24-month mark forfeits
    the *ganancia ocasional* 15% flat and is taxed at your marginal rate (up to 39%). Each such
    sale is exactly the over-trading the whole project exists to prevent (§2) — so the panel
    surfaces it, not as a scold but as a mirror. Holding period is measured lot-by-lot (PEPS):
    the shortest consumed lot drives ``holding_days`` (the most damning number).

    Returns {count, gain_cop, days_to_long_term, last: {asset, date, holding_days, gain_cop} |
    None}. ``count`` = number of this-year disposals with any renta-ordinaria consumption;
    ``gain_cop`` = their summed ordinary COP gain; ``last`` = the most recent one.
    """
    built = built if built is not None else build_tax_lots(conn, settings)
    months = int(settings.raw.get("fiscal", {}).get("long_term_months", 24))
    year = datetime.now(timezone.utc).year

    acquired_by_lot = {lot["lot_id"]: lot["acquired_at"] for lot in built["lots"]}
    disp_by_id = {d["disposal_id"]: d for d in built["disposals"]}

    # Group renta-ordinaria consumption rows by disposal (this year only).
    per_disposal: dict[str, dict[str, Any]] = {}
    for c in built["consumption"]:
        if c["regime"] != "renta_ordinaria":
            continue
        disp = disp_by_id.get(c["disposal_id"])
        if disp is None or str(disp["disposed_at"])[:4] != str(year):
            continue
        acquired = acquired_by_lot.get(c["lot_id"])
        if acquired is None:
            continue
        held_days = int((pd.Timestamp(disp["disposed_at"]) - pd.Timestamp(acquired)).days)
        agg = per_disposal.setdefault(
            c["disposal_id"],
            {"asset": disp["asset"], "date": str(disp["disposed_at"])[:10],
             "disposed_at": disp["disposed_at"], "holding_days": held_days, "gain_cop": 0.0},
        )
        agg["holding_days"] = min(agg["holding_days"], held_days)  # shortest lot = worst case
        if c["gain_cop"] is not None:
            agg["gain_cop"] += c["gain_cop"]

    events = list(per_disposal.values())
    last = max(events, key=lambda e: e["disposed_at"]) if events else None
    return {
        "count": len(events),
        "gain_cop": float(sum(e["gain_cop"] for e in events)),
        "days_to_long_term": months * 30,  # informational: how far a fresh buy is from the 15% rate
        "last": None if last is None else {
            "asset": last["asset"], "date": last["date"],
            "holding_days": last["holding_days"], "gain_cop": last["gain_cop"],
        },
    }


# --- Fase D: thesis journal + exit ladder (RESEARCH.md §17) --------------------------------------


def persist_thesis_and_ladder(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    """Persist thesis-journal and exit-ladder entries from config (``fiscal.thesis_log`` /
    ``fiscal.exit_ladder`` in settings.local.yaml). Idempotent; the loader rejects a thesis with
    no falsification criteria (RESEARCH.md §17). Returns row counts."""
    from db.loader import upsert_exit_ladder, upsert_thesis_log

    fiscal = settings.raw.get("fiscal", {})
    today = datetime.now(timezone.utc).date().isoformat()
    theses = [{
        "thesis_id": t.get("thesis_id") or f"thesis:{i}",
        "asset": t.get("asset"),
        "written_at": t.get("written_at") or today,
        "thesis": t.get("thesis"),
        "horizon": t.get("horizon"),
        "falsification_criteria": t.get("falsification_criteria"),
        "probability": t.get("probability"),
        "review_date": t.get("review_date"),
        "status": t.get("status") or "vigente",
        "outcome_reasoning": t.get("outcome_reasoning"),
        "outcome_pnl": t.get("outcome_pnl"),
    } for i, t in enumerate(fiscal.get("thesis_log", []) or [])]
    ladders = [{
        "ladder_id": lad.get("ladder_id") or f"ladder:{i}",
        "asset": lad.get("asset"),
        "tranche_n": lad.get("tranche_n", i + 1),
        "trigger_type": lad.get("trigger_type"),
        "trigger_value": lad.get("trigger_value"),
        "pct_to_sell": lad.get("pct_to_sell"),
        "executed_at": lad.get("executed_at"),
    } for i, lad in enumerate(fiscal.get("exit_ladder", []) or [])]
    return {
        "thesis": upsert_thesis_log(conn, theses),
        "ladder": upsert_exit_ladder(conn, ladders),
    }


def thesis_log_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Written thesis journal with a review-overdue flag (RESEARCH.md §17).

    ``review_overdue`` = the review date has passed while the thesis is still open
    (``vigente``/``en_revision``) — the prompt to revisit it. Columns: [asset, thesis, horizon,
    falsification_criteria, status, review_date, days_to_review, review_overdue].
    """
    rows = conn.execute(
        "SELECT asset, thesis, horizon, falsification_criteria, status, review_date "
        "FROM thesis_log ORDER BY asset"
    ).fetchall()
    today = datetime.now(timezone.utc).date()
    out = []
    for asset, thesis, horizon, falsif, status, review in rows:
        days = None
        overdue = False
        if review:
            rd = pd.Timestamp(review).date()
            days = (rd - today).days
            overdue = days < 0 and status in ("vigente", "en_revision")
        out.append({
            "asset": asset, "thesis": thesis, "horizon": horizon,
            "falsification_criteria": falsif, "status": status, "review_date": review,
            "days_to_review": days, "review_overdue": overdue,
        })
    return pd.DataFrame(out, columns=["asset", "thesis", "horizon", "falsification_criteria",
                                      "status", "review_date", "days_to_review", "review_overdue"])


def exit_ladder_table(
    conn: sqlite3.Connection, settings: Settings, built: dict | None = None
) -> pd.DataFrame:
    """Exit-ladder tranches with progress toward each trigger (RESEARCH.md §17).

    Progress is computed for ``precio`` (current price vs. target) and ``multiplo`` (current
    price / average entry cost vs. target multiple); ``fecha`` triggers are shown as-is.
    ``reached`` marks a fired, unexecuted tranche. Columns: [asset, tranche_n, trigger_type,
    trigger_value, pct_to_sell, detail, progress_pct, reached, executed].
    """
    rows = conn.execute(
        "SELECT asset, tranche_n, trigger_type, trigger_value, pct_to_sell, executed_at "
        "FROM exit_ladder ORDER BY asset, tranche_n"
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["asset", "tranche_n", "trigger_type", "trigger_value",
                                     "pct_to_sell", "detail", "progress_pct", "reached", "executed"])
    pnl = cop_usd_pnl_table(conn, settings, built=built)
    cost_by = {r["asset"]: (r["cost_usd"], r["units"]) for r in pnl.to_dict("records")}
    out = []
    for asset, tranche_n, ttype, tval, pct, executed_at in rows:
        price = _current_price_usd(conn, settings, asset)
        progress = None
        reached = False
        detail = ""
        if ttype == "precio" and price:
            progress = price / tval * 100.0 if tval else None
            reached = price >= tval
            detail = f"${price:,.0f} / ${tval:,.0f}"
        elif ttype == "multiplo" and price and asset in cost_by and cost_by[asset][1]:
            avg = cost_by[asset][0] / cost_by[asset][1]
            mult = price / avg if avg else None
            if mult is not None:
                progress = mult / tval * 100.0 if tval else None
                reached = mult >= tval
                detail = f"{mult:.2f}x / {tval:.2f}x"
        elif ttype == "fecha":
            detail = str(tval)
        out.append({
            "asset": asset, "tranche_n": tranche_n, "trigger_type": ttype, "trigger_value": tval,
            "pct_to_sell": pct, "detail": detail, "progress_pct": progress,
            "reached": reached and executed_at is None, "executed": executed_at is not None,
        })
    return pd.DataFrame(out)


# --- Fase E: Formulario 160 (foreign assets) — RESEARCH.md §17 -----------------------------------


def form160_check(
    conn: sqlite3.Connection, settings: Settings, built: dict | None = None
) -> dict[str, Any]:
    """Are you required to file Formulario 160 (foreign assets)? (RESEARCH.md §17, Fase E).

    Binance is a foreign asset; if its **patrimonial value at cost** (Art. 74/271) exceeds
    ``foreign_assets_form160_uvt`` × UVT of the year, filing is mandatory (informational, but
    omission is penalised). The UVT is set by the DIAN yearly and is ``null`` by default → the
    check returns ``has_uvt=False`` and "no calculable" rather than a fabricated number. The
    threshold is measured at Jan 1; here it uses current cost as a proxy (a caveat, not a filing).

    Returns {has_uvt, obligado, patrimonio_cop, threshold_cop, uvt, uvt_threshold, year}.
    """
    fiscal = settings.raw.get("fiscal", {})
    uvt_threshold = float(fiscal.get("foreign_assets_form160_uvt", 2000))
    uvt_by_year = fiscal.get("uvt_cop", {}) or {}
    year = datetime.now(timezone.utc).year
    patrimonio_cop = float(cop_usd_pnl_table(conn, settings, built=built).attrs.get("patrimonio_cop", 0.0))
    uvt = uvt_by_year.get(year, uvt_by_year.get(str(year)))
    if not uvt:
        return {"has_uvt": False, "obligado": None, "patrimonio_cop": patrimonio_cop,
                "threshold_cop": None, "uvt": None, "uvt_threshold": uvt_threshold, "year": year}
    threshold_cop = uvt_threshold * float(uvt)
    return {"has_uvt": True, "obligado": patrimonio_cop > threshold_cop,
            "patrimonio_cop": patrimonio_cop, "threshold_cop": threshold_cop,
            "uvt": float(uvt), "uvt_threshold": uvt_threshold, "year": year}

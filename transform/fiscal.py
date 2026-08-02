"""Colombian tax layer: lots, PEPS disposals and COP/USD PnL (FISCAL.md Paso 6).

Builds **immutable acquisition lots** from the real ``trades``, freezing each cost in COP at
the acquisition-day TRM (Art. 269 E.T.), and matches sells against the oldest lots (PEPS/FIFO)
into disposals with a **regime split**: a lot held ≥ ``long_term_months`` (24) at disposal is
*ganancia ocasional* (15%), otherwise *renta ordinaria* (progressive). The headline product is
:func:`cop_usd_pnl_table` — the same position shown in **both** currencies, because a loss in
USD can be a **taxable gain in COP** when the peso devalues between buy and sell.

**Estimates, not filings** (FISCAL.md §0): every figure is ``ESTIMADO — verificar con contador``.
Personal data: never synced to a shared/cloud DB, hidden under ``PUBLIC_MODE``.

Pure/deterministic given the DB; no network. Prices use CoinGecko latest (the panel's source);
the TRM is the ``banrep`` series. Crypto→crypto Converts are a known blind spot (they are dropped
upstream in ``parse_convert`` — FISCAL.md §2.2); only what reaches ``trades`` is lotted here.
"""

from __future__ import annotations

import sqlite3
from collections import deque
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
            disposals.append({
                "disposal_id": str(trade_id), "asset": base, "disposed_at": t.isoformat(),
                "units": float(amount), "proceeds_usd": proceeds_usd, "trm_disposal": trm_t,
                "proceeds_cop": proceeds_cop, "kind": "venta",
            })
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

    return {"lots": lots, "disposals": disposals, "consumption": consumption}


def _current_price_usd(conn: sqlite3.Connection, settings: Settings, asset: str) -> float | None:
    """Latest CoinGecko USD price for a symbol (the panel's price source)."""
    cid = next((a["coingecko_id"] for a in settings.assets if a["symbol"] == asset), None)
    if not cid:
        return None
    obs = latest_observation(conn, "coingecko", f"{cid}:price")
    return None if obs is None else float(obs[1])


def cop_usd_pnl_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Unrealized PnL per asset in **USD and COP side by side** (FISCAL.md §6.2).

    Cost is the frozen COP cost of the lots still open (Art. 269); current value is the live
    CoinGecko price × the latest TRM. The point: ``pnl_usd`` and ``pnl_cop`` can disagree in
    sign — a USD loss becomes a taxable COP gain if the peso devalued. Rows only for assets
    with open lots and a current price. Attrs: ``trm_now``, ``patrimonio_cop`` (Σ frozen cost —
    the Art. 74/271 fiscal net worth, at COST not market).

    Columns: [asset, units, cost_usd, cost_cop, value_usd, value_cop, pnl_usd, pnl_cop].
    """
    built = build_tax_lots(conn, settings)
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


def persist_tax_lots(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    """Build and upsert lots/disposals/consumption (idempotent). Returns row counts."""
    from db.loader import upsert_tax_disposals, upsert_tax_lot_consumption, upsert_tax_lots

    built = build_tax_lots(conn, settings)
    return {
        "lots": upsert_tax_lots(conn, built["lots"]),
        "disposals": upsert_tax_disposals(conn, built["disposals"]),
        "consumption": upsert_tax_lot_consumption(conn, built["consumption"]),
    }

"""Rally-quality transforms (CLAUDE.md sections 2, 8 — checklist level 2).

Distinguishes a conviction rally (new money) from a mechanical one (short covering),
and flags crowded positioning via a funding-rate z-score.

Two layers, mirroring transform/indicators.py:
1. Pure functions (``funding_zscore``, ``rally_state``) — no I/O, unit-tested.
2. A table builder (``market_structure_table``) that reads stored derivatives
   observations via db.queries and assembles the per-asset view the dashboard renders.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from core.config import Settings
from db.queries import series_history
from transform.indicators import pct_change_over_days

# Rally-state labels (price direction x open-interest direction).
RALLY_CONVICTION = "conviccion"      # price up + OI up   -> new money long
RALLY_MECHANICAL = "mecanico"        # price up + OI down -> short covering, fragile
RALLY_DISTRIBUTION = "distribucion"  # price down + OI up -> new shorts
RALLY_CAPITULATION = "capitulacion"  # price down + OI down -> long liquidation easing


def funding_zscore(funding: pd.Series, window_days: int = 90) -> float | None:
    """Z-score of the latest funding rate over a trailing window.

    Args:
        funding: Funding-rate series indexed by tz-aware datetimes, ascending.
        window_days: Trailing window for the mean/std (default 90, §8).

    Returns:
        (latest - mean) / std over the window, or None if there are fewer than two
        points in the window or the window is flat (std == 0). A population std
        (ddof=0) is used so a single-value window is handled by the length guard.

    Reference thresholds (§8): z > 2 = crowded longs (cascade risk); z < -2 =
    crowded shorts (short-squeeze conditions).
    """
    series = funding.dropna()
    if len(series) < 2:
        return None
    latest_ts = series.index[-1]
    window = series.loc[series.index > latest_ts - pd.Timedelta(days=window_days)]
    if len(window) < 2:
        return None
    std = window.std(ddof=0)
    if std == 0 or pd.isna(std):
        return None
    return float((series.iloc[-1] - window.mean()) / std)


def rally_state(price_change_pct: float | None, oi_change_pct: float | None) -> str | None:
    """Classify rally quality from price vs. open-interest direction.

    Args:
        price_change_pct: Percent change in price over the window.
        oi_change_pct: Percent change in open interest over the same window.

    Returns:
        One of the RALLY_* labels, or None if either input is missing. The two
        named cases in §8 are ``conviccion`` (price↑ OI↑) and ``mecanico``
        (price↑ OI↓); the down-move quadrants are labelled for completeness.
    """
    if price_change_pct is None or oi_change_pct is None or pd.isna(price_change_pct) or pd.isna(oi_change_pct):
        return None
    price_up = price_change_pct > 0
    oi_up = oi_change_pct > 0
    if price_up:
        return RALLY_CONVICTION if oi_up else RALLY_MECHANICAL
    return RALLY_DISTRIBUTION if oi_up else RALLY_CAPITULATION


def _combined_oi(conn: sqlite3.Connection, symbol: str, exchanges: list[str]) -> pd.Series:
    """Sum USD open interest across exchanges into one daily series (aligned by date)."""
    frames = [series_history(conn, "derivatives", f"{symbol}:oi:{ex}") for ex in exchanges]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.Series(dtype="float64")
    combined = pd.concat(frames, axis=1).sum(axis=1, min_count=1)
    return combined.dropna()


def _primary_series(conn: sqlite3.Connection, symbol: str, kind: str, exchanges: list[str]) -> pd.Series:
    """Return the first non-empty per-exchange series for a kind (funding/close)."""
    for ex in exchanges:
        s = series_history(conn, "derivatives", f"{symbol}:{kind}:{ex}")
        if not s.dropna().empty:
            return s
    return pd.Series(dtype="float64")


def market_structure_table(
    conn: sqlite3.Connection, settings: Settings, divergence_days: int = 7
) -> pd.DataFrame:
    """Per-asset market-structure view: funding z-score, price/OI change, rally state.

    Reads stored derivatives observations. Assets without perp data are omitted.
    Price and OI changes are measured over ``divergence_days`` from the perp venue.
    """
    cfg = settings.source("derivatives")
    exchanges: list[str] = cfg.get("exchanges", ["binance", "bybit"])
    window: int = int(cfg.get("zscore_window_days", 90))

    rows: list[dict[str, Any]] = []
    for asset in settings.assets:
        symbol = asset["symbol"]
        funding = _primary_series(conn, symbol, "funding", exchanges)
        close = _primary_series(conn, symbol, "close", exchanges)
        oi = _combined_oi(conn, symbol, exchanges)

        if funding.dropna().empty and oi.empty and close.dropna().empty:
            continue  # no perp market for this asset

        price_chg = pct_change_over_days(close, divergence_days)
        oi_chg = pct_change_over_days(oi, divergence_days)
        rows.append(
            {
                "symbol": symbol,
                "funding_z": funding_zscore(funding, window),
                "funding_latest": None if funding.dropna().empty else float(funding.dropna().iloc[-1]),
                "price_chg": price_chg,
                "oi_chg": oi_chg,
                "rally_state": rally_state(price_chg, oi_chg),
            }
        )
    return pd.DataFrame(rows)

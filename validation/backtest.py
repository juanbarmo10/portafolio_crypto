"""Signal validation framework (CLAUDE.md sections 8, 9 phase 3).

Given a price series and the dates a signal fired, compare the signal's **forward
returns** (7/30/90 d) against the unconditional baseline (all dates), with a bootstrap
significance test. Includes one concrete signal — the funding z-score — built from data
already in the DB.

Honesty over cherry-picking (§9): report the result whatever it is, including "no edge"
and "insufficient data". Rolling z-scores are point-in-time (trailing window only), so
there is no look-ahead in the signal itself.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from core.config import Settings
from db.queries import series_history
from validation.metrics import bootstrap_mean_diff_pvalue, forward_return


def rolling_zscore(series: pd.Series, window_days: int) -> pd.Series:
    """Point-in-time z-score at each timestamp using only the trailing window.

    No look-ahead: z(t) uses observations in (t - window, t]. Points with fewer than two
    prior observations or a flat window are dropped.
    """
    s = series.dropna()
    values: dict[pd.Timestamp, float] = {}
    for t in s.index:
        window = s.loc[(s.index > t - pd.Timedelta(days=window_days)) & (s.index <= t)]
        if len(window) < 2:
            continue
        std = window.std(ddof=0)
        if std == 0 or pd.isna(std):
            continue
        values[t] = (s.loc[t] - window.mean()) / std
    return pd.Series(values, dtype=float)


def evaluate_signal(
    prices: pd.Series, signal_dates: list[pd.Timestamp], horizons: tuple[int, ...] = (7, 30, 90)
) -> dict[int, dict[str, Any]]:
    """Forward-return stats for a signal vs. the unconditional baseline, per horizon.

    Returns ``{horizon: {n_signal, n_baseline, mean_signal, mean_baseline, edge, pvalue}}``
    where ``edge`` = mean_signal - mean_baseline (percentage points).
    """
    all_dates = list(prices.dropna().index)
    out: dict[int, dict[str, Any]] = {}
    for h in horizons:
        sig = [r for d in signal_dates if (r := forward_return(prices, d, h)) is not None]
        base = [r for d in all_dates if (r := forward_return(prices, d, h)) is not None]
        mean_sig = float(np.mean(sig)) if sig else None
        mean_base = float(np.mean(base)) if base else None
        out[h] = {
            "n_signal": len(sig),
            "n_baseline": len(base),
            "mean_signal": mean_sig,
            "mean_baseline": mean_base,
            "edge": None if mean_sig is None or mean_base is None else mean_sig - mean_base,
            "pvalue": bootstrap_mean_diff_pvalue(sig, base),
        }
    return out


def _first_series(conn: sqlite3.Connection, asset: str, kind: str, exchanges: list[str]) -> pd.Series:
    for ex in exchanges:
        s = series_history(conn, "derivatives", f"{asset}:{kind}:{ex}")
        if not s.dropna().empty:
            return s
    return pd.Series(dtype="float64")


def funding_zscore_backtest(
    conn: sqlite3.Connection,
    settings: Settings,
    asset: str,
    z_threshold: float = 1.5,
    window_days: int = 90,
    horizons: tuple[int, ...] = (7,),
) -> dict[int, dict[str, Any]] | None:
    """Backtest: does a high funding z-score precede weaker forward returns?

    Signal = dates where the trailing funding z-score >= ``z_threshold`` (crowded longs).
    Price = the perp daily close (same venue). Hypothesis: crowded longs -> lower forward
    returns (negative edge). Returns None if the asset lacks funding/close history.
    """
    exchanges = settings.source("derivatives").get("exchanges", ["binance", "bybit"])
    funding = _first_series(conn, asset, "funding", exchanges)
    close = _first_series(conn, asset, "close", exchanges)
    if funding.dropna().empty or close.dropna().empty:
        return None

    z = rolling_zscore(funding, window_days)
    signal_dates = list(z.index[z >= z_threshold])
    return evaluate_signal(close, signal_dates, horizons)

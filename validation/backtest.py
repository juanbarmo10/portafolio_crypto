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
    where ``edge`` = mean_signal - mean_baseline (percentage points). The baseline is the
    set of **non-signal** dates (disjoint from the signal), so the permutation test compares
    two exchangeable groups instead of a set against its own superset (which biases it).
    """
    signal_set = set(signal_dates)
    non_signal_dates = [d for d in prices.dropna().index if d not in signal_set]
    out: dict[int, dict[str, Any]] = {}
    for h in horizons:
        sig = [r for d in signal_dates if (r := forward_return(prices, d, h)) is not None]
        base = [r for d in non_signal_dates if (r := forward_return(prices, d, h)) is not None]
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


# ---------------------------------------------------------------------------
# G5-e — DCA allocator backtest (Parte G): compare monthly-DCA strategies A/B/D/E
# on the SAME cashflow, using Binance daily history (§9: not CoinGecko). Point-in-time.
# ---------------------------------------------------------------------------


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI(period) — point-in-time (each value uses only prior prices).

    Standard convention: an all-gains window (avg loss 0) gives RSI 100 (rs→∞); a fully flat
    window (avg gain and loss both 0) is NaN. Plain division yields exactly this.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0).rolling(period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss
    return 100.0 - 100.0 / (1.0 + rs)


def _binance_price_matrix(conn: sqlite3.Connection, settings: Settings, min_days: int) -> pd.DataFrame:
    """[date × asset] daily Binance close for the DCA assets with >= min_days history."""
    assets = [
        k for k in settings.raw.get("portfolio", {}).get("target_weights_asset", {}) if k != "CASH"
    ]
    cols: dict[str, pd.Series] = {}
    for a in assets:
        s = series_history(conn, "spot_prices", f"{a}:spot_close:binance").dropna()
        if len(s) >= min_days:
            cols[a] = s
    if not cols:
        return pd.DataFrame()
    return pd.concat(cols, axis=1, sort=False).sort_index().ffill()


def _simulate_dca(prices: pd.DataFrame, month_starts: list, alloc_fn, contribution: float) -> float | None:
    """Accumulate tokens applying ``alloc_fn(t, avail, values_now, C)`` each month.

    ``avail`` = assets that already exist (non-NaN price) at ``t`` — so no strategy wastes budget
    on a token that hadn't listed yet (which would otherwise flatter the single-asset pickers).
    """
    tokens = dict.fromkeys(prices.columns, 0.0)
    contributed = 0.0
    for t in month_starts:
        px = prices.loc[t]
        avail = [a for a in prices.columns if not pd.isna(px[a])]
        if not avail:
            continue
        values = {a: tokens[a] * px[a] for a in avail}
        for asset, usd in alloc_fn(t, avail, values, contribution).items():
            if usd > 0 and asset in avail and px[asset] > 0:
                tokens[asset] += usd / px[asset]
        contributed += contribution
    final = prices.iloc[-1]
    value = sum(tokens[a] * final[a] for a in prices.columns if not pd.isna(final[a]))
    return (value / contributed - 1.0) * 100.0 if contributed > 0 else None


def dca_allocator_backtest(
    conn: sqlite3.Connection,
    settings: Settings,
    contribution: float = 200.0,
    min_history_days: int = 400,
    bootstrap: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """G5-e: compare monthly-DCA strategies on the same cashflow (Binance history, §9).

    Strategies (each allocates ``contribution`` USD every month, point-in-time):
      * **A_fixed**   — equal split across all assets (the honest DCA baseline).
      * **B_drift**   — rebalance by contribution toward ``target_weights_asset`` (buy the
        under-weighted; the allocator's core).
      * **D_rsi**     — put it all in the lowest-RSI(14) asset (the "buy the oversold" pick).
      * **E_momentum**— put it all in the highest-90d-momentum asset (the opposite bet, a control).
    (Strategy C, drift+veto, needs historical *fundamental* invalidation data —TVL/unlocks— that
    price history alone cannot reconstruct, so it is out of scope for this price-only backtest.)

    Reports the full-period return per strategy plus a bootstrap over **start months** (the DCA
    result depends heavily on when you start), with pairwise P(strategy beats baseline). Uses
    Binance prices, never CoinGecko (§9). ``has_data`` False without enough history.
    """
    prices = _binance_price_matrix(conn, settings, min_history_days)
    if prices.empty or len(prices) < min_history_days:
        return {"has_data": False}

    # First trading day of each calendar month (group by year/month to avoid the tz-dropping
    # to_period warning); chronological because (year, month) sorts naturally.
    idx = pd.Series(prices.index, index=prices.index)
    month_starts = idx.groupby([prices.index.year, prices.index.month]).first().tolist()

    raw_t = {
        k: v
        for k, v in settings.raw.get("portfolio", {}).get("target_weights_asset", {}).items()
        if k in prices.columns
    }
    tot = sum(raw_t.values()) or 1.0
    target_w = {k: v / tot * 100.0 for k, v in raw_t.items()}

    rsi_m = pd.DataFrame({a: _rsi(prices[a]) for a in prices.columns})
    mom_m = prices.pct_change(90)

    def alloc_fixed(t, avail, values, c):
        return dict.fromkeys(avail, c / len(avail))

    def alloc_drift(t, avail, values, c):
        # Renormalize the target weights over the assets that exist at t.
        tw = {a: target_w.get(a, 0.0) for a in avail}
        tsum = sum(tw.values()) or 1.0
        tw = {a: v / tsum * 100.0 for a, v in tw.items()}
        total = sum(values.values()) + c
        needs = {a: max(0.0, tw[a] / 100.0 * total - values.get(a, 0.0)) for a in avail}
        tn = sum(needs.values())
        if tn <= 0:
            return dict.fromkeys(avail, c / len(avail))
        scale = min(1.0, c / tn)
        return {a: needs[a] * scale for a in needs}

    def alloc_rsi(t, avail, values, c):
        row = rsi_m.loc[t][avail].dropna()
        return {row.idxmin(): c} if not row.empty else {}

    def alloc_mom(t, avail, values, c):
        row = mom_m.loc[t][avail].dropna()
        return {row.idxmax(): c} if not row.empty else {}

    strategies = {"A_fixed": alloc_fixed, "B_drift": alloc_drift, "D_rsi": alloc_rsi, "E_momentum": alloc_mom}
    point = {name: _simulate_dca(prices, month_starts, fn, contribution) for name, fn in strategies.items()}

    rng = np.random.default_rng(seed)
    n = len(month_starts)
    samples: list[dict[str, float | None]] = []
    if n >= 24:
        hi = n - 12  # leave at least ~12 months to accumulate
        for _ in range(bootstrap):
            ms = month_starts[int(rng.integers(0, hi)) :]
            samples.append({name: _simulate_dca(prices, ms, fn, contribution) for name, fn in strategies.items()})

    def _mean(name: str) -> float | None:
        vals = [s[name] for s in samples if s[name] is not None]
        return float(np.mean(vals)) if vals else None

    def _prob(a: str, b: str) -> float | None:
        diffs = [s[a] - s[b] for s in samples if s[a] is not None and s[b] is not None]
        return float(np.mean([1.0 if d > 0 else 0.0 for d in diffs])) if diffs else None

    return {
        "has_data": True,
        "assets": list(prices.columns),
        "n_months": n,
        "history_start": str(prices.index[0].date()),
        "contribution": contribution,
        "point_return_pct": point,
        "bootstrap_n": len(samples),
        "bootstrap_mean_pct": {name: _mean(name) for name in strategies},
        "prob_beats_baseline": {
            "B_drift_vs_A_fixed": _prob("B_drift", "A_fixed"),
            "D_rsi_vs_B_drift": _prob("D_rsi", "B_drift"),
            "E_momentum_vs_B_drift": _prob("E_momentum", "B_drift"),
        },
    }

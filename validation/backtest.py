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
from validation.metrics import benjamini_hochberg, bootstrap_mean_diff_pvalue, forward_return


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


# ---------------------------------------------------------------------------
# C1 — Signal battery + FDR (Parte C): validate several level-1/2 signals at once
# and control the false-discovery rate across them (§9 multiple testing). Honest about
# which signals lack enough history to conclude.
# ---------------------------------------------------------------------------


def _rising_dates(driver: pd.Series, days: int) -> list[pd.Timestamp]:
    """Dates where ``driver`` is higher than its value ~``days`` ago (point-in-time, as-of)."""
    d = driver.dropna()
    d = d[~d.index.duplicated(keep="last")]
    dates: list[pd.Timestamp] = []
    for t in d.index:
        past = d.loc[d.index <= t - pd.Timedelta(days=days)]
        if not past.empty and float(d.loc[t]) > float(past.iloc[-1]):
            dates.append(t)
    return dates


def _episodes(dates: list[pd.Timestamp], min_gap_days: int) -> list[pd.Timestamp]:
    """Thin dates so consecutive kept ones are >= ``min_gap_days`` apart.

    A rising-regime signal fires on almost every day, so raw daily counts are wildly
    autocorrelated and the permutation test overstates significance. Keeping one date per
    ``horizon``-sized gap makes the forward-return windows non-overlapping and ``n`` an honest
    count of near-independent episodes.
    """
    last: pd.Timestamp | None = None
    kept: list[pd.Timestamp] = []
    for d in sorted(dates):
        if last is None or (d - last) >= pd.Timedelta(days=min_gap_days):
            kept.append(d)
            last = d
    return kept


def _battery_eval(
    price: pd.Series, signal_dates: list[pd.Timestamp], horizon: int, min_signal: int
) -> dict[str, Any]:
    """Edge + p-value of one signal, on episode-thinned (non-overlapping) signal dates, with
    the baseline restricted to the signal's active window (conditional vs. unconditional over
    the *same* period, not vs. all of history)."""
    price = price.dropna()
    signal_dates = _episodes(signal_dates, horizon)  # non-overlapping forward windows
    if price.empty or not signal_dates:
        return {"status": "insufficient", "n_signal": 0, "n_baseline": 0, "edge": None, "pvalue": None}
    lo, hi = min(signal_dates), max(signal_dates)
    window = price.loc[(price.index >= lo) & (price.index <= hi + pd.Timedelta(days=horizon))]
    ev = evaluate_signal(window, signal_dates, (horizon,))[horizon]
    ok = ev["n_signal"] >= min_signal and ev["n_baseline"] >= 30 and ev["pvalue"] is not None
    return {
        "status": "ok" if ok else "insufficient",
        "n_signal": ev["n_signal"],
        "n_baseline": ev["n_baseline"],
        "edge": ev["edge"],
        "pvalue": ev["pvalue"],
    }


def signal_battery(
    conn: sqlite3.Connection,
    settings: Settings,
    horizon: int = 30,
    fdr_alpha: float = 0.10,
    min_signal: int = 8,
) -> dict[str, Any]:
    """C1: validate a family of level-1/2 signals at one horizon, with FDR control.

    Each signal fires on point-in-time dates (§9: macro on ``ts_release``) and is scored by
    the forward return of a price proxy vs. the unconditional baseline over the **same**
    window (:func:`evaluate_signal`), giving an edge (pp) and a permutation p-value. Because
    several signals are tested at once, Benjamini-Hochberg FDR (:func:`benjamini_hochberg`)
    is applied across the ones with enough history; signals too short to conclude are flagged
    ``insufficient`` and excluded from the correction (honesty over cherry-picking).

    Prices use **Binance** spot close (§9, not CoinGecko). Returns ``{has_data, horizon,
    fdr_alpha, n_tested, signals: [{name, hypothesis, direction, n_signal, edge, pvalue,
    qvalue, significant, status}]}``.
    """
    from transform.indicators import fed_net_liquidity_series

    btc = series_history(conn, "spot_prices", "BTC:spot_close:binance").dropna()
    eth = series_history(conn, "spot_prices", "ETH:spot_close:binance").dropna()

    signals: list[dict[str, Any]] = []

    def add(name: str, hypothesis: str, direction: int, price: pd.Series,
            signal_dates: list[pd.Timestamp]) -> None:
        row = {"name": name, "hypothesis": hypothesis, "direction": direction,
               "qvalue": None, "significant": None}
        row.update(_battery_eval(price, signal_dates, horizon, min_signal))
        signals.append(row)

    # 1 — Net liquidity rising (A1), point-in-time via release dates → BTC up (risk-on).
    nl = fed_net_liquidity_series(conn, settings, by_release=True)
    add("Liquidez neta ↑", "liquidez neta subiendo → BTC ↑ (risk-on)", +1, btc,
        _rising_dates(nl, 28))

    # 2 — Aggregate stablecoin mcap rising (A7) → dry powder entering → BTC up.
    stbl = series_history(conn, "defillama", "stablecoins:total_mcap")
    add("Stablecoins ↑", "capital aparcado creciendo (dry powder) → BTC ↑", +1, btc,
        _rising_dates(stbl, 28))

    # 3 — BTC ETF net-inflow streak (trailing 5-day sum > 0) → BTC up.
    etf = series_history(conn, "farside", "etf:btc:total").sort_index()
    streak = etf.rolling(5).sum() if not etf.dropna().empty else pd.Series(dtype="float64")
    add("Flujos ETF racha+", "entradas ETF sostenidas → BTC ↑", +1, btc,
        list(streak.index[streak > 0]))

    # 4 — ETH/BTC ratio rising (A9) → rotation persists (momentum in the ratio itself).
    both = pd.concat({"eth": eth, "btc": btc}, axis=1, sort=False).sort_index().ffill().dropna()
    ratio = (both["eth"] / both["btc"]).rename("ethbtc") if not both.empty else pd.Series(dtype="float64")
    add("Rotación ETH/BTC ↑", "ETH/BTC subiendo → la rotación persiste", +1, ratio,
        _rising_dates(ratio, 28))

    # 5 — Crowded longs: high BTC funding z → weaker forward returns. Short history (~90 d)
    # → usually flagged insufficient; kept so the battery is honest about what it can't judge.
    exchanges = settings.source("derivatives").get("exchanges", ["binance", "bybit"])
    funding = _first_series(conn, "BTC", "funding", exchanges)
    z = rolling_zscore(funding, 90) if not funding.dropna().empty else pd.Series(dtype="float64")
    add("Funding z alto (BTC)", "largos hacinados → BTC ↓ (frágil)", -1, btc,
        list(z.index[z >= 1.5]))

    # FDR across the signals with enough history; the rest stay 'insufficient'.
    testable = [s for s in signals if s["status"] == "ok"]
    for s, (q, rej) in zip(testable, benjamini_hochberg([s["pvalue"] for s in testable], fdr_alpha)):
        s["qvalue"], s["significant"] = q, rej

    return {
        "has_data": any(s["status"] == "ok" for s in signals),
        "horizon": horizon,
        "fdr_alpha": fdr_alpha,
        "n_tested": len(testable),
        "signals": signals,
    }


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

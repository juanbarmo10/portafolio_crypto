"""Portfolio-risk transforms (IDEAS_MEJORAS Parte B, B2/B3; CLAUDE.md sections 2, 5).

Real diversification is by **failure mode**, not by ticker count (§5). With the backfilled
daily price history this quantifies it: a **correlation matrix** and **beta to BTC** (B2) plus
**volatility, max drawdown, concentration (HHI) and risk contribution** (B3) — all from our own
stored prices, no new dependency. Ten tokens correlated 0.9 with BTC are *one* bet; this makes
that visible with numbers instead of intuition.

Two layers, mirroring the other transform modules:
1. A shared returns builder (``_returns_frame``) — daily returns of the held (alias-merged,
   priced) positions, value weights, and a BTC benchmark, all aligned on common dates.
2. DB-backed views: ``correlation_matrix`` (B2) and ``portfolio_risk_summary`` (B2+B3).

All statistics use sample moments (ddof=1) and annualize with 365 days (crypto trades daily,
not 252). Missing/short history yields empty results, never an exception.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from core.config import Settings
from db.queries import series_history
from transform.indicators import _cid_for_symbol, _holdings_value_matrix, holdings_table


def _daily_returns(conn: sqlite3.Connection, cid: str, days: int) -> pd.Series:
    """Simple daily returns of a CoinGecko price series over the trailing ``days``."""
    prices = series_history(conn, "coingecko", f"{cid}:price").dropna()
    if len(prices) < 3:
        return pd.Series(dtype="float64")
    rets = prices.pct_change().dropna()
    if rets.empty:
        return rets
    cutoff = rets.index[-1] - pd.Timedelta(days=days)
    return rets[rets.index >= cutoff]


def _max_drawdown(value: pd.Series) -> float | None:
    """Max drawdown (%) of a value path: min over t of value_t / running-peak_t − 1."""
    v = value.dropna()
    if len(v) < 2:
        return None
    dd = v / v.cummax() - 1.0
    return float(dd.min()) * 100.0


def _returns_frame(
    conn: sqlite3.Connection, settings: Settings, holdings: pd.DataFrame, days: int
) -> tuple[pd.DataFrame, dict[str, float], pd.Series]:
    """Aligned daily-returns matrix of held positions + value weights + BTC benchmark.

    Positions are **alias-merged** (WBETH → ETH, §B4) and non-cash; a position is included only
    if it has a price series. Returns (rets[date × symbol], {symbol: weight}, btc_bench_returns),
    all sharing the same dates (inner join). Empty frame if fewer than one priced position.
    """
    if holdings.empty:
        return pd.DataFrame(), {}, pd.Series(dtype="float64")
    aliases = settings.source("binance_account").get("symbol_aliases", {}) or {}
    positions: dict[str, float] = {}
    for r in holdings.to_dict("records"):
        val = r["value_usd"]
        if r["is_cash"] or val is None or pd.isna(val):
            continue
        sym = aliases.get(r["asset"], r["asset"])
        positions[sym] = positions.get(sym, 0.0) + float(val)

    series: dict[str, pd.Series] = {}
    for sym in positions:
        cid = _cid_for_symbol(sym, settings)
        if not cid:
            continue
        r = _daily_returns(conn, cid, days)
        if len(r) >= 2:
            series[sym] = r
    if not series:
        return pd.DataFrame(), {}, pd.Series(dtype="float64")

    frame = dict(series)
    btc = _daily_returns(conn, "bitcoin", days)
    if not btc.empty:
        frame["__BTC_BENCH__"] = btc
    allf = pd.concat(frame, axis=1, join="inner", sort=False).dropna()
    if allf.empty:
        return pd.DataFrame(), {}, pd.Series(dtype="float64")

    if "__BTC_BENCH__" in allf.columns:
        btc_bench = allf["__BTC_BENCH__"]
        rets = allf.drop(columns="__BTC_BENCH__")
    else:
        btc_bench = pd.Series(dtype="float64")
        rets = allf
    if rets.shape[1] == 0:
        return pd.DataFrame(), {}, pd.Series(dtype="float64")

    total = sum(positions[s] for s in rets.columns)
    weights = {s: positions[s] / total for s in rets.columns} if total > 0 else {}
    return rets, weights, btc_bench


def _risk_window(settings: Settings, days: int | None) -> int:
    return int(days or settings.raw.get("indicators", {}).get("risk", {}).get("window_days", 90))


def correlation_matrix(
    conn: sqlite3.Connection,
    settings: Settings,
    holdings: pd.DataFrame | None = None,
    days: int | None = None,
) -> pd.DataFrame:
    """B2: correlation matrix of held assets' daily returns (empty if < 2 priced positions)."""
    if holdings is None:
        holdings = holdings_table(conn, settings)
    rets, _, _ = _returns_frame(conn, settings, holdings, _risk_window(settings, days))
    if rets.shape[1] < 2:
        return pd.DataFrame()
    return rets.corr()


def portfolio_risk_summary(
    conn: sqlite3.Connection,
    settings: Settings,
    holdings: pd.DataFrame | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """B2+B3: portfolio risk — vol, beta, HHI, drawdown, and per-asset risk contribution.

    Per asset: value weight, annualized volatility, beta to BTC, and **risk contribution**
    ``RC_i = w_i·(Σw)_i / (wᵀΣw)`` (share of portfolio variance — a 15%-weight position can be
    40% of the risk if it is the most volatile). Portfolio: annualized volatility ``√(wᵀΣw)``,
    value-weighted beta to BTC, **HHI** ``Σ w²`` with effective-N ``1/HHI``, average pairwise
    correlation, high-correlation pairs (|ρ| ≥ config), and max drawdown of the risky value path.
    ``has_data`` False when there is not enough priced history.
    """
    riskcfg = settings.raw.get("indicators", {}).get("risk", {})
    days = _risk_window(settings, days)
    ann = float(riskcfg.get("annualization_days", 365))
    hi = float(riskcfg.get("high_correlation", 0.8))

    if holdings is None:
        holdings = holdings_table(conn, settings)
    rets, weights, btc = _returns_frame(conn, settings, holdings, days)
    if rets.empty or not weights:
        return {"has_data": False}

    syms = list(rets.columns)
    w = np.array([weights[s] for s in syms])
    cov = rets.cov().values * ann  # annualized covariance (ddof=1)
    port_var = float(w @ cov @ w)
    port_vol = math.sqrt(port_var) if port_var > 0 else 0.0
    marginal = cov @ w
    rc = (w * marginal / port_var) if port_var > 0 else np.zeros_like(w)
    vol_asset = {s: float(rets[s].std() * math.sqrt(ann)) for s in syms}

    # Beta to BTC (cov and var both ddof=1 → the sample factor cancels).
    beta: dict[str, float] = {}
    port_beta: float | None = None
    if not btc.empty and float(btc.var()) > 0:
        bvar = float(btc.var())
        for s in syms:
            beta[s] = float(rets[s].cov(btc) / bvar)
        port_beta = float(sum(weights[s] * beta[s] for s in syms))

    hhi = float(np.sum(w * w))
    eff_n = (1.0 / hhi) if hhi > 0 else None

    avg_corr: float | None = None
    hi_pairs: list[tuple[str, str, float]] = []
    if len(syms) >= 2:
        cm = rets.corr()
        n = len(syms)
        avg_corr = float((cm.values.sum() - n) / (n * (n - 1)))
        for i in range(n):
            for j in range(i + 1, n):
                c = float(cm.iloc[i, j])
                if abs(c) >= hi:
                    hi_pairs.append((syms[i], syms[j], c))

    matrix, _cash = _holdings_value_matrix(conn, settings, holdings)
    mdd = _max_drawdown(matrix.sum(axis=1, min_count=1)) if not matrix.empty else None

    per_asset = [
        {
            "symbol": s,
            "weight_pct": weights[s] * 100.0,
            "vol_annual_pct": vol_asset[s] * 100.0,
            "beta_btc": beta.get(s),
            "risk_contribution_pct": float(rc[k] * 100.0),
        }
        for k, s in enumerate(syms)
    ]
    per_asset.sort(key=lambda x: x["risk_contribution_pct"], reverse=True)

    return {
        "has_data": True,
        "n_assets": len(syms),
        "window_days": days,
        "port_vol_annual_pct": port_vol * 100.0,
        "port_beta_btc": port_beta,
        "hhi": hhi,
        "effective_n": eff_n,
        "avg_correlation": avg_corr,
        "max_drawdown_pct": mdd,
        "high_corr_pairs": hi_pairs,
        "per_asset": per_asset,
    }

"""Validation metrics (CLAUDE.md sections 8, 9 phase 3).

Pure functions for signal validation: forward returns and a bootstrap (permutation)
significance test. No I/O — unit-tested with synthetic data.

Look-ahead discipline (§9) lives in the caller: signals over macro data must be built
from ``ts_release``, never ``ts``. These metrics only see prices and dates.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def forward_return(prices: pd.Series, at: pd.Timestamp, horizon_days: int) -> float | None:
    """Percent return from the price at/just-before ``at`` to ~``horizon_days`` later.

    Entry = last price with index <= ``at``; exit = first price with index >= entry +
    horizon. None if there is no entry, no exit that far out, or entry price is zero.
    Tolerant of gaps (as-of lookups).
    """
    series = prices.dropna()
    if series.empty:
        return None
    entry = series.loc[series.index <= at]
    if entry.empty:
        return None
    p0 = entry.iloc[-1]
    target = entry.index[-1] + pd.Timedelta(days=horizon_days)
    exit_ = series.loc[series.index >= target]
    if exit_.empty or p0 == 0:
        return None
    return float((exit_.iloc[0] / p0 - 1.0) * 100.0)


def bootstrap_mean_diff_pvalue(
    signal_returns: list[float],
    baseline_returns: list[float],
    n: int = 10_000,
    seed: int = 0,
) -> float | None:
    """Two-sided permutation p-value that mean(signal) != mean(baseline).

    Pools both samples and reshuffles the group labels ``n`` times; the p-value is the
    fraction of shuffles whose absolute mean-difference is >= the observed one. This
    tests whether the signal's forward returns differ from the unconditional (baseline)
    returns beyond chance. None if either sample is empty.
    """
    sig = np.asarray([r for r in signal_returns if r is not None], dtype=float)
    base = np.asarray([r for r in baseline_returns if r is not None], dtype=float)
    if sig.size == 0 or base.size == 0:
        return None

    observed = sig.mean() - base.mean()
    pool = np.concatenate([sig, base])
    n_sig = sig.size

    rng = np.random.default_rng(seed)
    order = rng.random((n, pool.size)).argsort(axis=1)  # n random permutations
    permuted = pool[order]
    diffs = permuted[:, :n_sig].mean(axis=1) - permuted[:, n_sig:].mean(axis=1)
    return float((np.abs(diffs) >= abs(observed)).mean())

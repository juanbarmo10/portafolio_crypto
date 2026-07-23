"""Tests for the validation framework (CLAUDE.md sections 8, 10)."""

from __future__ import annotations

import pandas as pd
import pytest

from validation.backtest import evaluate_signal, rolling_zscore
from validation.metrics import bootstrap_mean_diff_pvalue, forward_return


def _daily(values: list[float], end: str = "2026-07-23") -> pd.Series:
    idx = pd.date_range(end=end, periods=len(values), freq="D", tz="UTC")
    return pd.Series(values, index=idx, dtype="float64")


# --- forward_return ----------------------------------------------------------


def test_forward_return_basic() -> None:
    prices = _daily([100, 101, 102, 103, 104, 105, 106, 110])
    at = prices.index[0]
    assert forward_return(prices, at, 7) == pytest.approx(10.0)  # 100 -> 110 over 7 days


def test_forward_return_insufficient_future() -> None:
    prices = _daily([100, 110])
    assert forward_return(prices, prices.index[0], 7) is None  # no price 7d out


def test_forward_return_empty() -> None:
    assert forward_return(pd.Series(dtype="float64"), pd.Timestamp("2026-07-01", tz="UTC"), 7) is None


# --- bootstrap_mean_diff_pvalue ----------------------------------------------


def test_bootstrap_identical_means_high_p() -> None:
    same = list(range(20))
    assert bootstrap_mean_diff_pvalue(same, same, n=2000) == 1.0  # observed diff 0


def test_bootstrap_clear_separation_low_p() -> None:
    p = bootstrap_mean_diff_pvalue([10.0] * 20, [-10.0] * 20, n=2000)
    assert p is not None and p < 0.05


def test_bootstrap_empty_returns_none() -> None:
    assert bootstrap_mean_diff_pvalue([], [1.0], n=100) is None


# --- rolling_zscore ----------------------------------------------------------


def test_rolling_zscore_is_point_in_time() -> None:
    # Flat then a spike; the spike's z is large and computed from trailing data only.
    s = _daily([0.001] * 10 + [0.05])
    z = rolling_zscore(s, window_days=90)
    assert z.iloc[-1] > 2
    # First point has no trailing window -> excluded.
    assert s.index[0] not in z.index


# --- evaluate_signal ---------------------------------------------------------


def test_evaluate_signal_shapes() -> None:
    prices = _daily(list(range(100, 140)))  # 40 days, rising
    signal_dates = [prices.index[0], prices.index[5]]
    res = evaluate_signal(prices, signal_dates, horizons=(7,))
    assert res[7]["n_signal"] == 2
    assert res[7]["mean_signal"] is not None
    assert res[7]["edge"] is not None

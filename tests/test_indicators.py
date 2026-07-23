"""Tests for transform.indicators pure functions (CLAUDE.md section 10)."""

from __future__ import annotations

import pandas as pd
import pytest

from transform.indicators import (
    btc_dominance,
    distance_to_ath,
    dilution_ratio,
    mc_tvl_ratio,
    pct_change_over_days,
)


def _daily_series(values: list[float], end: str = "2026-07-22") -> pd.Series:
    """Build a daily-indexed, tz-aware Series ending at ``end`` (ascending)."""
    idx = pd.date_range(end=end, periods=len(values), freq="D", tz="UTC")
    return pd.Series(values, index=idx, dtype="float64")


# --- btc_dominance -----------------------------------------------------------


def test_btc_dominance_basic() -> None:
    caps = {"bitcoin": 60.0, "ethereum": 30.0, "solana": 10.0}
    assert btc_dominance(caps) == pytest.approx(60.0)


def test_btc_dominance_missing_btc_returns_none() -> None:
    assert btc_dominance({"ethereum": 10.0}) is None


def test_btc_dominance_ignores_none_values() -> None:
    caps = {"bitcoin": 50.0, "ethereum": None, "solana": 50.0}
    assert btc_dominance(caps) == pytest.approx(50.0)


def test_btc_dominance_zero_total_returns_none() -> None:
    assert btc_dominance({"bitcoin": 0.0}) is None


# --- pct_change_over_days ----------------------------------------------------


def test_pct_change_7d() -> None:
    # 8 daily points; value 7 days before the last is index 0.
    s = _daily_series([100, 101, 102, 103, 104, 105, 106, 110])
    assert pct_change_over_days(s, 7) == pytest.approx(10.0)


def test_pct_change_insufficient_history_returns_none() -> None:
    s = _daily_series([100, 110])  # only 2 days; 7d window has no past ref
    assert pct_change_over_days(s, 7) is None


def test_pct_change_empty_returns_none() -> None:
    assert pct_change_over_days(pd.Series(dtype="float64"), 1) is None


def test_pct_change_asof_tolerates_gaps() -> None:
    # Gap: last is day 30, past ref on-or-before day 23 is the day-20 point.
    idx = pd.to_datetime(
        ["2026-07-01", "2026-07-20", "2026-07-30"], utc=True
    )
    s = pd.Series([100.0, 200.0, 220.0], index=idx)
    # 7 days before 07-30 is 07-23; last value <= 07-23 is 200 (07-20).
    assert pct_change_over_days(s, 7) == pytest.approx(10.0)


def test_pct_change_zero_past_value_returns_none() -> None:
    s = _daily_series([0, 1, 2, 3, 4, 5, 6, 7])
    assert pct_change_over_days(s, 7) is None


# --- distance_to_ath ---------------------------------------------------------


def test_distance_to_ath_below() -> None:
    assert distance_to_ath(50.0, 100.0) == pytest.approx(-50.0)


def test_distance_to_ath_at_high() -> None:
    assert distance_to_ath(100.0, 100.0) == pytest.approx(0.0)


def test_distance_to_ath_missing_returns_none() -> None:
    assert distance_to_ath(None, 100.0) is None
    assert distance_to_ath(50.0, 0.0) is None


# --- mc_tvl_ratio ------------------------------------------------------------


def test_mc_tvl_ratio_basic() -> None:
    assert mc_tvl_ratio(2_000_000.0, 1_000_000.0) == pytest.approx(2.0)


def test_mc_tvl_ratio_zero_tvl_returns_none() -> None:
    assert mc_tvl_ratio(1.0, 0.0) is None


# --- dilution_ratio ----------------------------------------------------------


def test_dilution_ratio_basic() -> None:
    assert dilution_ratio(40.0, 100.0) == pytest.approx(0.4)


def test_dilution_ratio_missing_max_returns_none() -> None:
    # Uncapped tokens (max_supply None) have no defined dilution ratio.
    assert dilution_ratio(1000.0, None) is None

"""Tests for transform.rally_quality pure functions (CLAUDE.md sections 8, 10)."""

from __future__ import annotations

import pandas as pd

from transform.rally_quality import (
    RALLY_CAPITULATION,
    RALLY_CONVICTION,
    RALLY_DISTRIBUTION,
    RALLY_MECHANICAL,
    funding_zscore,
    rally_state,
)


def _funding_series(values: list[float], end: str = "2026-07-23") -> pd.Series:
    """8h-cadence funding series ending at ``end`` (ascending, tz-aware)."""
    idx = pd.date_range(end=end, periods=len(values), freq="8h", tz="UTC")
    return pd.Series(values, index=idx, dtype="float64")


# --- funding_zscore ----------------------------------------------------------


def test_funding_zscore_positive_extreme() -> None:
    # Flat history then a spike -> large positive z.
    s = _funding_series([0.0001] * 20 + [0.01])
    z = funding_zscore(s, window_days=90)
    assert z is not None and z > 2  # crowded longs (§8)


def test_funding_zscore_negative_extreme() -> None:
    s = _funding_series([0.0001] * 20 + [-0.01])
    z = funding_zscore(s, window_days=90)
    assert z is not None and z < -2  # crowded shorts


def test_funding_zscore_flat_returns_none() -> None:
    assert funding_zscore(_funding_series([0.0001] * 10)) is None  # std == 0


def test_funding_zscore_insufficient_returns_none() -> None:
    assert funding_zscore(_funding_series([0.0001])) is None
    assert funding_zscore(pd.Series(dtype="float64")) is None


def test_funding_zscore_window_excludes_old() -> None:
    # Old outlier outside the window must not affect the z of the recent, flat stretch.
    idx = pd.to_datetime(["2020-01-01"], utc=True).append(
        pd.date_range(end="2026-07-23", periods=6, freq="8h", tz="UTC")
    )
    s = pd.Series([99.0, 0.0001, 0.0002, 0.00015, 0.0001, 0.0002, 0.00012], index=idx)
    z = funding_zscore(s, window_days=90)
    assert z is not None and abs(z) < 3  # the 2020 outlier is excluded


# --- rally_state -------------------------------------------------------------


def test_rally_state_quadrants() -> None:
    assert rally_state(5.0, 3.0) == RALLY_CONVICTION      # price up, OI up
    assert rally_state(5.0, -3.0) == RALLY_MECHANICAL     # price up, OI down
    assert rally_state(-5.0, 3.0) == RALLY_DISTRIBUTION   # price down, OI up
    assert rally_state(-5.0, -3.0) == RALLY_CAPITULATION  # price down, OI down


def test_rally_state_missing_returns_none() -> None:
    assert rally_state(None, 3.0) is None
    assert rally_state(5.0, None) is None
    assert rally_state(float("nan"), 3.0) is None

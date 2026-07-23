"""Tests for ingest.fred.parse_initial_releases — the look-ahead guard (CLAUDE.md section 9).

No API key required: parse_initial_releases is a pure function over the FRED
``output_type=4`` ("initial release only") observation list.
"""

from __future__ import annotations

from ingest.fred import parse_initial_releases


def _observations() -> list[dict]:
    """Initial-release observations as FRED returns them with output_type=4."""
    return [
        {"realtime_start": "2015-02-26", "date": "2015-01-01", "value": "233.7"},
        {"realtime_start": "2026-07-14", "date": "2026-06-01", "value": "332.568"},
    ]


def test_maps_reference_and_release_dates() -> None:
    """ts is the reference date, ts_release is the first-publication date."""
    out = parse_initial_releases(_observations(), "CPIAUCSL")
    row = out[out["ts"].str.startswith("2026-06")].iloc[0]
    assert row["value"] == 332.568
    assert row["ts"].startswith("2026-06-01")
    assert row["ts_release"].startswith("2026-07-14")  # published weeks after reference
    assert row["source"] == "fred"
    assert row["series_id"] == "CPIAUCSL"


def test_sorted_ascending_by_reference_date() -> None:
    out = parse_initial_releases(_observations(), "CPIAUCSL")
    assert list(out["ts"]) == sorted(out["ts"])
    assert len(out) == 2


def test_missing_values_dropped() -> None:
    """FRED's '.' sentinel for a missing value is dropped, not stored as a number."""
    obs = [
        {"realtime_start": "2015-02-26", "date": "2015-01-01", "value": "."},
        {"realtime_start": "2026-07-14", "date": "2026-06-01", "value": "332.568"},
    ]
    out = parse_initial_releases(obs, "CPIAUCSL")
    assert len(out) == 1
    assert out.iloc[0]["ts"].startswith("2026-06")


def test_duplicate_reference_keeps_earliest_release() -> None:
    """If a reference date appears twice (bisection boundary), keep the earliest release."""
    obs = [
        {"realtime_start": "2026-08-15", "date": "2026-06-01", "value": "333.0"},  # revision
        {"realtime_start": "2026-07-14", "date": "2026-06-01", "value": "332.568"},  # initial
    ]
    out = parse_initial_releases(obs, "CPIAUCSL")
    assert len(out) == 1
    assert out.iloc[0]["value"] == 332.568
    assert out.iloc[0]["ts_release"].startswith("2026-07-14")


def test_empty_input_returns_empty_contract() -> None:
    out = parse_initial_releases([], "X")
    assert list(out.columns) == ["source", "series_id", "ts", "ts_release", "value"]
    assert out.empty

"""Tests for the Farside ETF-flows parser (CLAUDE.md sections 9, 10).

Runs against a frozen HTML fixture so a structure change on Farside makes these
fail loudly, and asserts pinned values so a silent parsing regression is caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db.loader import OBSERVATION_COLUMNS
from ingest.etf_flows import _parse_flow, parse_farside

FIXTURE = Path(__file__).parent / "fixtures" / "farside_btc.html"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# --- _parse_flow -------------------------------------------------------------


def test_parse_flow_variants() -> None:
    assert _parse_flow("1,234.5") == 1234.5
    assert _parse_flow("(63.3)") == -63.3      # parenthesised outflow
    assert _parse_flow("0.0") == 0.0
    assert _parse_flow("-") is None            # no-data sentinel
    assert _parse_flow("") is None
    assert _parse_flow("nan") is None


# --- parse_farside against the frozen fixture --------------------------------


def test_parse_farside_columns_and_shape() -> None:
    df = parse_farside(_fixture_html(), "btc")
    assert list(df.columns) == OBSERVATION_COLUMNS
    assert len(df) > 0
    assert (df["source"] == "farside").all()


def test_parse_farside_pinned_total() -> None:
    df = parse_farside(_fixture_html(), "btc")
    total = df[df["series_id"] == "etf:btc:total"].set_index("ts")["value"]
    # Pinned from the frozen fixture (22 Jul 2026 total inflow, USD millions).
    assert total["2026-07-22T00:00:00+00:00"] == pytest.approx(69.1)
    assert total["2026-07-23T00:00:00+00:00"] == pytest.approx(0.0)


def test_parse_farside_has_issuer_series() -> None:
    df = parse_farside(_fixture_html(), "btc")
    series = set(df["series_id"])
    assert "etf:btc:IBIT" in series   # BlackRock, always present
    assert "etf:btc:total" in series
    # Summary rows (Total/Average/Maximum/Minimum) must not become data rows:
    # every ts parses as an ISO date.
    assert all(ts.startswith("20") for ts in df["ts"])


# --- fail-loudly behavior (§9) ----------------------------------------------


def test_parse_farside_raises_on_no_date_table() -> None:
    html = "<html><body><table><tr><td>foo</td><td>bar</td><td>baz</td></tr></table></body></html>"
    with pytest.raises(ValueError):
        parse_farside(html, "btc")


def test_parse_farside_raises_without_total_column() -> None:
    # A date table (>=3 cols) but no 'Total' header -> loud failure, not silent empty.
    html = (
        "<html><body><table>"
        "<tr><th>Date</th><th>IBIT</th><th>FBTC</th></tr>"
        "<tr><td>06 Jul 2026</td><td>209.4</td><td>9.7</td></tr>"
        "</table></body></html>"
    )
    with pytest.raises(ValueError):
        parse_farside(html, "btc")

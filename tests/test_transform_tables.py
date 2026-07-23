"""Smoke tests for transform.indicators DB-backed table builders (CLAUDE.md section 8).

These seed a temporary DB with a few observations and assert the assembled tables
have the right shape and values — no network involved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from core.config import load_settings
from db.loader import init_db, upsert_observations
from transform.indicators import dca_status, portfolio_table, thesis_tvl_table


@pytest.fixture()
def settings():
    """The real settings object (asset universe drives the table builders)."""
    load_settings.cache_clear()
    s = load_settings()
    yield s
    load_settings.cache_clear()


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = init_db(tmp_path / "t.db")
    yield connection
    connection.close()


def _obs(series_id: str, ts: str, value: float, source: str = "coingecko") -> dict:
    return {
        "source": source,
        "series_id": series_id,
        "ts": ts,
        "ts_release": None,
        "value": value,
    }


def test_portfolio_table_computes_dilution_and_dominance(conn, settings) -> None:
    df = pd.DataFrame(
        [
            _obs("bitcoin:price", "2026-07-22T00:00:00+00:00", 65000.0),
            _obs("bitcoin:market_cap", "2026-07-22T00:00:00+00:00", 1_300_000_000_000.0),
            _obs("bitcoin:ath", "2026-07-22T00:00:00+00:00", 100_000.0),
            _obs("bitcoin:circulating_supply", "2026-07-22T00:00:00+00:00", 19_700_000.0),
            _obs("bitcoin:max_supply", "2026-07-22T00:00:00+00:00", 21_000_000.0),
            _obs("ethereum:market_cap", "2026-07-22T00:00:00+00:00", 260_000_000_000.0),
        ]
    )
    upsert_observations(conn, df)

    table = portfolio_table(conn, settings)
    btc = table[table["symbol"] == "BTC"].iloc[0]
    assert btc["price"] == 65000.0
    # dist to ATH: 65000/100000 - 1 = -35%
    assert btc["dist_ath"] == pytest.approx(-35.0)
    # dilution 19.7M / 21M ~ 0.938 -> not a risk (> 0.6 default)
    assert btc["dilution_ratio"] == pytest.approx(19_700_000.0 / 21_000_000.0)
    assert not btc["dilution_risk"]
    # dominance present in attrs
    assert table.attrs["btc_dominance"] is not None


def test_thesis_tvl_table_7d_change(conn, settings) -> None:
    # Aave TVL: 100 eight days ago, 110 today -> +10% over 7d.
    df = pd.DataFrame(
        [
            _obs("aave:tvl", "2026-07-14T00:00:00+00:00", 100.0, source="defillama"),
            _obs("aave:tvl", "2026-07-22T00:00:00+00:00", 110.0, source="defillama"),
        ]
    )
    upsert_observations(conn, df)
    table = thesis_tvl_table(conn, settings)
    aave = table[table["symbol"] == "AAVE"].iloc[0]
    assert aave["tvl"] == 110.0
    assert aave["tvl_chg_7d"] == pytest.approx(10.0)


def test_dca_status_empty_plan(conn, settings) -> None:
    status = dca_status(conn, settings)
    assert status["deployed_usd"] == 0.0
    assert status["planned_usd"] == 0.0
    assert status["next_tranche"] is None
    assert status["monthly_min_usd"] == 200

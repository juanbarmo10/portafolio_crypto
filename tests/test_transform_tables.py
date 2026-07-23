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
from transform.indicators import dca_status, macro_table, portfolio_table, thesis_tvl_table


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


def test_macro_table_change_and_labels(conn, settings) -> None:
    # CPI: previous 100.0 then latest 101.0 -> +1.0% MoM change.
    df = pd.DataFrame(
        [
            _obs("CPIAUCSL", "2026-05-01T00:00:00+00:00", 100.0, source="fred"),
            _obs("CPIAUCSL", "2026-06-01T00:00:00+00:00", 101.0, source="fred"),
        ]
    )
    upsert_observations(conn, df)
    table = macro_table(conn, settings)
    cpi = table[table["series_id"] == "CPIAUCSL"].iloc[0]
    assert cpi["label"] == "CPI"  # display label from config, not the raw key
    assert cpi["description"]  # non-empty tooltip text
    assert cpi["value"] == 101.0
    assert cpi["change_pct"] == pytest.approx(1.0)


def test_macro_nfp_absolute_change(conn, settings) -> None:
    # NFP is configured as absolute display: report jobs added (in thousands).
    df = pd.DataFrame(
        [
            _obs("PAYEMS", "2026-05-01T00:00:00+00:00", 158834.0, source="fred"),
            _obs("PAYEMS", "2026-06-01T00:00:00+00:00", 158984.0, source="fred"),
        ]
    )
    upsert_observations(conn, df)
    table = macro_table(conn, settings)
    nfp = table[table["series_id"] == "PAYEMS"].iloc[0]
    assert nfp["change_display"] == "absolute"
    assert nfp["change_unit"] == "K"
    assert nfp["change_abs"] == pytest.approx(150.0)  # +150K jobs


def test_macro_change_none_without_previous(conn, settings) -> None:
    upsert_observations(
        conn,
        pd.DataFrame([_obs("DFF", "2026-07-21T00:00:00+00:00", 3.63, source="fred")]),
    )
    table = macro_table(conn, settings)
    dff = table[table["series_id"] == "DFF"].iloc[0]
    assert dff["value"] == 3.63
    assert dff["change_pct"] is None  # single observation -> no change


def test_dca_status_empty_plan(conn, settings) -> None:
    status = dca_status(conn, settings)
    assert status["deployed_usd"] == 0.0
    assert status["planned_usd"] == 0.0
    assert status["next_tranche"] is None
    assert status["monthly_min_usd"] == 200

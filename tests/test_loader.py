"""Tests for db.loader: schema creation and idempotent upserts (CLAUDE.md section 6).

The idempotency test is the Phase 0 guarantee: running ingestion twice must not
duplicate rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from db.loader import (
    OBSERVATION_COLUMNS,
    TRADE_COLUMNS,
    init_db,
    upsert_exit_ladder,
    upsert_observations,
    upsert_thesis_log,
    upsert_trades,
)


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A fresh initialized database on a temporary path."""
    connection = init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def _sample_df() -> pd.DataFrame:
    """Two observations, one with a null ts_release (macro look-ahead field)."""
    return pd.DataFrame(
        [
            {
                "source": "fred",
                "series_id": "CPIAUCSL",
                "ts": "2026-06-01T00:00:00+00:00",
                "ts_release": "2026-07-15T00:00:00+00:00",
                "value": 320.1,
            },
            {
                "source": "coingecko",
                "series_id": "ethereum:price",
                "ts": "2026-07-22T00:00:00+00:00",
                "ts_release": None,
                "value": 2240.0,
            },
        ]
    )


def test_schema_creates_expected_tables(conn: sqlite3.Connection) -> None:
    """init_db creates all five tables from schema.sql."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"observations", "events", "alerts_log", "dca_plan", "exit_rules"} <= names


def test_apply_schema_is_idempotent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Re-initializing an existing DB does not error (IF NOT EXISTS)."""
    conn2 = init_db(tmp_path / "test.db")  # same path, second init
    conn2.close()  # no exception == pass


def test_upsert_inserts_rows(conn: sqlite3.Connection) -> None:
    """A first upsert writes exactly the submitted rows."""
    n = upsert_observations(conn, _sample_df())
    assert n == 2
    count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count == 2


def test_upsert_is_idempotent(conn: sqlite3.Connection) -> None:
    """Running the same upsert twice yields no duplicate rows (section 6)."""
    df = _sample_df()
    upsert_observations(conn, df)
    upsert_observations(conn, df)
    count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count == 2


def test_upsert_updates_value_on_conflict(conn: sqlite3.Connection) -> None:
    """A conflicting key updates value/ts_release rather than inserting."""
    df = _sample_df()
    upsert_observations(conn, df)

    df2 = df.copy()
    df2.loc[df2["series_id"] == "ethereum:price", "value"] = 2500.0
    upsert_observations(conn, df2)

    value = conn.execute(
        "SELECT value FROM observations WHERE series_id = 'ethereum:price'"
    ).fetchone()[0]
    assert value == 2500.0
    count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count == 2


def test_upsert_missing_columns_raises(conn: sqlite3.Connection) -> None:
    """Missing required columns fail loudly (section 10: no silent failures)."""
    bad = pd.DataFrame([{"source": "x", "series_id": "y"}])
    with pytest.raises(ValueError):
        upsert_observations(conn, bad)


def test_null_ts_release_persists_as_null(conn: sqlite3.Connection) -> None:
    """A null ts_release round-trips as SQL NULL, not the string 'None'."""
    upsert_observations(conn, _sample_df())
    ts_release = conn.execute(
        "SELECT ts_release FROM observations WHERE series_id = 'ethereum:price'"
    ).fetchone()[0]
    assert ts_release is None


def test_observation_columns_contract() -> None:
    """The public column contract is the one ingesters must honor."""
    assert OBSERVATION_COLUMNS == ["source", "series_id", "ts", "ts_release", "value"]


def _trade_df(trade_id: str = "binance:1", cost: float = 65.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_id": trade_id,
                "exchange": "binance",
                "symbol": "BTC/USDT",
                "side": "buy",
                "ts": "2026-07-21T00:00:00+00:00",
                "price": 65000.0,
                "amount": 0.001,
                "cost": cost,
                "fee": 0.065,
                "fee_currency": "USDT",
            }
        ]
    )


def test_upsert_trades_inserts_and_is_idempotent(conn: sqlite3.Connection) -> None:
    assert upsert_trades(conn, _trade_df()) == 1
    upsert_trades(conn, _trade_df())  # same trade_id again
    count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert count == 1  # no duplicate


def test_upsert_trades_updates_on_conflict(conn: sqlite3.Connection) -> None:
    upsert_trades(conn, _trade_df(cost=65.0))
    upsert_trades(conn, _trade_df(cost=70.0))  # correction to same trade
    cost = conn.execute("SELECT cost FROM trades WHERE trade_id='binance:1'").fetchone()[0]
    assert cost == 70.0


def test_upsert_trades_missing_columns_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        upsert_trades(conn, pd.DataFrame([{"trade_id": "x"}]))


def test_trade_columns_contract() -> None:
    assert TRADE_COLUMNS[0] == "trade_id" and "fee_currency" in TRADE_COLUMNS


# --- Colombian tax layer: thesis journal + exit ladder (FISCAL.md §5) --------


def _thesis(thesis_id: str = "t1", falsification: str = "flujos ETF negativos") -> dict:
    return {
        "thesis_id": thesis_id, "asset": "BTC", "written_at": "2026-01-01T00:00:00+00:00",
        "thesis": "reserva de valor", "horizon": "trimestres",
        "falsification_criteria": falsification, "probability": 0.6,
        "review_date": "2026-09-01", "status": "vigente",
        "outcome_reasoning": None, "outcome_pnl": None,
    }


def test_upsert_thesis_log_requires_falsification(conn: sqlite3.Connection) -> None:
    assert upsert_thesis_log(conn, [_thesis()]) == 1
    # A thesis with no (or blank) falsification criteria is rejected loudly (FISCAL.md §5).
    with pytest.raises(ValueError):
        upsert_thesis_log(conn, [_thesis("t2", falsification="   ")])


def test_upsert_exit_ladder_idempotent(conn: sqlite3.Connection) -> None:
    row = {"ladder_id": "l1", "asset": "BTC", "tranche_n": 1, "trigger_type": "multiplo",
           "trigger_value": 3.0, "pct_to_sell": 30.0, "executed_at": None}
    assert upsert_exit_ladder(conn, [row]) == 1
    upsert_exit_ladder(conn, [row])
    assert conn.execute("SELECT COUNT(*) FROM exit_ladder").fetchone()[0] == 1

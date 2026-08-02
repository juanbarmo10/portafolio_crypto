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
    upsert_observations,
    upsert_tax_disposals,
    upsert_tax_lot_consumption,
    upsert_tax_lots,
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


# --- Colombian tax layer (FISCAL.md) ----------------------------------------


def _lot(lot_id: str = "l1") -> dict:
    return {
        "lot_id": lot_id, "asset": "BTC", "acquired_at": "2025-10-01T00:00:00+00:00",
        "units": 0.01, "units_remaining": 0.01, "cost_usd": 600.0, "trm_acquisition": 4000.0,
        "cost_cop": 2_400_000.0, "fees_cop": 1000.0, "matures_at": "2027-10-01T00:00:00+00:00",
        "origin": "compra", "source_ref": "tr1",
    }


def test_upsert_tax_lots_idempotent(conn: sqlite3.Connection) -> None:
    assert upsert_tax_lots(conn, [_lot()]) == 1
    upsert_tax_lots(conn, [_lot()])  # re-run must not duplicate
    assert conn.execute("SELECT COUNT(*) FROM tax_lots").fetchone()[0] == 1
    row = conn.execute("SELECT cost_cop, matures_at FROM tax_lots WHERE lot_id='l1'").fetchone()
    assert row[0] == pytest.approx(2_400_000.0)


def test_upsert_tax_disposals_and_consumption(conn: sqlite3.Connection) -> None:
    upsert_tax_lots(conn, [_lot()])
    upsert_tax_disposals(conn, [{
        "disposal_id": "d1", "asset": "BTC", "disposed_at": "2026-05-01T00:00:00+00:00",
        "units": 0.01, "proceeds_usd": 700.0, "trm_disposal": 4200.0,
        "proceeds_cop": 2_940_000.0, "kind": "venta",
    }])
    upsert_tax_lot_consumption(conn, [{
        "disposal_id": "d1", "lot_id": "l1", "units": 0.01, "cost_cop": 2_400_000.0,
        "gain_cop": 540_000.0, "regime": "renta_ordinaria",
    }])
    assert conn.execute("SELECT COUNT(*) FROM tax_disposals").fetchone()[0] == 1
    g = conn.execute("SELECT gain_cop, regime FROM tax_lot_consumption").fetchone()
    assert g[0] == pytest.approx(540_000.0)
    assert g[1] == "renta_ordinaria"


def test_fiscal_upserts_empty_noop(conn: sqlite3.Connection) -> None:
    assert upsert_tax_lots(conn, []) == 0
    assert upsert_tax_disposals(conn, []) == 0
    assert upsert_tax_lot_consumption(conn, []) == 0

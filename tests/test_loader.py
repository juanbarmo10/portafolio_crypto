"""Tests for db.loader: schema creation and idempotent upserts (CLAUDE.md section 6).

The idempotency test is the Phase 0 guarantee: running ingestion twice must not
duplicate rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from db.loader import OBSERVATION_COLUMNS, init_db, upsert_observations


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

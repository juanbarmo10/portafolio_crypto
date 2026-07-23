"""Idempotent database loader (CLAUDE.md sections 6, 10).

Source-agnostic: every ingest module produces a DataFrame with the columns
``[source, series_id, ts, ts_release, value]`` and this loader upserts it. All
writes use ``INSERT ... ON CONFLICT DO UPDATE`` so the pipeline can be re-run
any number of times without duplicating rows (section 6: idempotency is mandatory).

Inputs (read):
    - db/schema.sql : DDL applied on connect (idempotent, uses IF NOT EXISTS).
    - pandas.DataFrame observations from ingest modules.

Outputs (write):
    - The SQLite database at Settings.db_path.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core.logging_setup import get_logger

log = get_logger(__name__)

SCHEMA_PATH: Path = Path(__file__).resolve().parent / "schema.sql"

# The canonical column contract for observation DataFrames (section 10).
OBSERVATION_COLUMNS = ["source", "series_id", "ts", "ts_release", "value"]

_UPSERT_SQL = """
INSERT INTO observations (source, series_id, ts, ts_release, value, ingested_at)
VALUES (:source, :series_id, :ts, :ts_release, :value, :ingested_at)
ON CONFLICT(source, series_id, ts) DO UPDATE SET
    ts_release  = excluded.ts_release,
    value       = excluded.value,
    ingested_at = excluded.ingested_at
"""


def _utc_now_iso() -> str:
    """Current UTC time as ISO8601 (section 10: timestamps are UTC ISO8601)."""
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled.

    Args:
        db_path: Destination database file. Parent directories are created.

    Returns:
        An open connection. The caller is responsible for closing it (or use
        it as a context manager for transaction handling).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply db/schema.sql to the connection. Idempotent (uses IF NOT EXISTS)."""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()
    log.debug("Schema applied from %s", SCHEMA_PATH)


def init_db(db_path: Path) -> sqlite3.Connection:
    """Connect and ensure the schema exists. Returns the open connection."""
    conn = connect(db_path)
    apply_schema(conn)
    return conn


def upsert_observations(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Upsert an observations DataFrame idempotently.

    Args:
        conn: Open connection with the schema applied.
        df: DataFrame with at least the columns in ``OBSERVATION_COLUMNS``.
            ``ts_release`` may be null; ``ingested_at`` is set here to now (UTC).

    Returns:
        Number of rows submitted (upserted). Re-running with the same rows does
        not create duplicates thanks to the ON CONFLICT clause.

    Raises:
        ValueError: If required columns are missing (fail loudly, section 10).
    """
    missing = [c for c in OBSERVATION_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Observation DataFrame missing required columns: {missing}. "
            f"Expected {OBSERVATION_COLUMNS}."
        )

    if df.empty:
        log.info("No observations to upsert (empty DataFrame).")
        return 0

    ingested_at = _utc_now_iso()
    records = [
        {
            "source": row.source,
            "series_id": row.series_id,
            "ts": row.ts,
            "ts_release": None if pd.isna(row.ts_release) else row.ts_release,
            "value": None if pd.isna(row.value) else float(row.value),
            "ingested_at": ingested_at,
        }
        for row in df[OBSERVATION_COLUMNS].itertuples(index=False)
    ]

    with conn:  # transaction: commit on success, rollback on error
        conn.executemany(_UPSERT_SQL, records)

    log.info("Upserted %d observations.", len(records))
    return len(records)

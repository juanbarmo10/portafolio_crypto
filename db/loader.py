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
from db.database import open_connection

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


def connect(db_path: Path):
    """Open the backend connection: PostgreSQL if DATABASE_URL is set, else SQLite.

    Args:
        db_path: SQLite destination file (used only in the SQLite fallback).

    Returns:
        An open connection with the sqlite3 interface (native sqlite3, or the Postgres
        facade from db.database). The caller closes it (or uses it as a context manager).
    """
    return open_connection(db_path)


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


TRADE_COLUMNS = [
    "trade_id", "exchange", "symbol", "side", "ts",
    "price", "amount", "cost", "fee", "fee_currency",
]

_UPSERT_TRADE_SQL = """
INSERT INTO trades
    (trade_id, exchange, symbol, side, ts, price, amount, cost, fee, fee_currency, ingested_at)
VALUES
    (:trade_id, :exchange, :symbol, :side, :ts, :price, :amount, :cost, :fee, :fee_currency, :ingested_at)
ON CONFLICT(trade_id) DO UPDATE SET
    price = excluded.price, amount = excluded.amount, cost = excluded.cost,
    fee = excluded.fee, fee_currency = excluded.fee_currency, ingested_at = excluded.ingested_at
"""


def upsert_trades(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Idempotently upsert executed trades (keyed by trade_id).

    Args:
        conn: Open connection with the schema applied.
        df: DataFrame with at least ``TRADE_COLUMNS``.

    Returns:
        Number of trade rows submitted. Re-running does not duplicate (ON CONFLICT).

    Raises:
        ValueError: If required columns are missing (fail loudly, section 10).
    """
    missing = [c for c in TRADE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Trades DataFrame missing required columns: {missing}.")
    if df.empty:
        log.info("No trades to upsert.")
        return 0

    ingested_at = _utc_now_iso()
    records = []
    for row in df[TRADE_COLUMNS].itertuples(index=False):
        record = {c: getattr(row, c) for c in TRADE_COLUMNS}
        for numeric in ("price", "amount", "cost", "fee"):
            record[numeric] = None if pd.isna(record[numeric]) else float(record[numeric])
        record["ingested_at"] = ingested_at
        records.append(record)

    with conn:
        conn.executemany(_UPSERT_TRADE_SQL, records)
    log.info("Upserted %d trades.", len(records))
    return len(records)


EVENT_COLUMNS = ["event_id", "category", "ts", "label", "payload"]

_UPSERT_EVENT_SQL = """
INSERT INTO events (event_id, category, ts, label, payload)
VALUES (:event_id, :category, :ts, :label, :payload)
ON CONFLICT(event_id) DO UPDATE SET
    category = excluded.category, ts = excluded.ts,
    label = excluded.label, payload = excluded.payload
"""


def upsert_events(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Idempotently upsert calendar events (keyed by event_id).

    Args:
        conn: Open connection with the schema applied.
        df: DataFrame with at least ``EVENT_COLUMNS`` (payload is a JSON string or None).

    Returns:
        Number of event rows submitted. Re-running does not duplicate (ON CONFLICT).

    Raises:
        ValueError: If required columns are missing (fail loudly, section 10).
    """
    missing = [c for c in EVENT_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Events DataFrame missing required columns: {missing}.")
    if df.empty:
        log.info("No events to upsert.")
        return 0

    records = [
        {c: (None if pd.isna(v := getattr(row, c)) else v) for c in EVENT_COLUMNS}
        for row in df[EVENT_COLUMNS].itertuples(index=False)
    ]
    with conn:
        conn.executemany(_UPSERT_EVENT_SQL, records)
    log.info("Upserted %d events.", len(records))
    return len(records)


CAPITAL_FLOW_COLUMNS = ["flow_id", "kind", "asset", "amount", "usd_value", "ts"]

_UPSERT_CAPITAL_FLOW_SQL = """
INSERT INTO capital_flows (flow_id, kind, asset, amount, usd_value, ts, ingested_at)
VALUES (:flow_id, :kind, :asset, :amount, :usd_value, :ts, :ingested_at)
ON CONFLICT(flow_id) DO UPDATE SET
    kind = excluded.kind, asset = excluded.asset, amount = excluded.amount,
    usd_value = excluded.usd_value, ts = excluded.ts, ingested_at = excluded.ingested_at
"""


def upsert_capital_flows(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Idempotently upsert fiat/crypto capital flows (keyed by flow_id)."""
    missing = [c for c in CAPITAL_FLOW_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Capital-flows DataFrame missing required columns: {missing}.")
    if df.empty:
        log.info("No capital flows to upsert.")
        return 0
    ingested_at = _utc_now_iso()
    records = []
    for row in df[CAPITAL_FLOW_COLUMNS].itertuples(index=False):
        record = {c: getattr(row, c) for c in CAPITAL_FLOW_COLUMNS}
        for numeric in ("amount", "usd_value"):
            record[numeric] = None if pd.isna(record[numeric]) else float(record[numeric])
        record["ingested_at"] = ingested_at
        records.append(record)
    with conn:
        conn.executemany(_UPSERT_CAPITAL_FLOW_SQL, records)
    log.info("Upserted %d capital flows.", len(records))
    return len(records)


# --- Colombian tax layer: thesis journal + exit ladder (FISCAL.md §5) -------
# PERSONAL, LOCAL only (never synced to a shared/cloud DB). The tax lots/disposals themselves
# are computed on demand from `trades` (transform/fiscal.py) and not persisted; only these two
# hand-written journals live in tables, since the views read them back.


def _upsert_records(conn: sqlite3.Connection, sql: str, cols: list[str], rows: list[dict],
                    with_ingested: bool, label: str) -> int:
    """Shared idempotent upsert from a list of dict rows (keyed by the table's primary key)."""
    if not rows:
        log.info("No %s to upsert.", label)
        return 0
    ingested_at = _utc_now_iso()
    records = []
    for r in rows:
        rec = {c: (None if pd.isna(r.get(c)) else r.get(c)) for c in cols}
        if with_ingested:
            rec["ingested_at"] = ingested_at
        records.append(rec)
    with conn:
        conn.executemany(sql, records)
    log.info("Upserted %d %s.", len(records), label)
    return len(records)


THESIS_LOG_COLUMNS = [
    "thesis_id", "asset", "written_at", "thesis", "horizon", "falsification_criteria",
    "probability", "review_date", "status", "outcome_reasoning", "outcome_pnl",
]

_UPSERT_THESIS_SQL = """
INSERT INTO thesis_log (thesis_id, asset, written_at, thesis, horizon, falsification_criteria,
    probability, review_date, status, outcome_reasoning, outcome_pnl)
VALUES (:thesis_id, :asset, :written_at, :thesis, :horizon, :falsification_criteria,
    :probability, :review_date, :status, :outcome_reasoning, :outcome_pnl)
ON CONFLICT(thesis_id) DO UPDATE SET
    asset = excluded.asset, written_at = excluded.written_at, thesis = excluded.thesis,
    horizon = excluded.horizon, falsification_criteria = excluded.falsification_criteria,
    probability = excluded.probability, review_date = excluded.review_date,
    status = excluded.status, outcome_reasoning = excluded.outcome_reasoning,
    outcome_pnl = excluded.outcome_pnl
"""

EXIT_LADDER_COLUMNS = [
    "ladder_id", "asset", "tranche_n", "trigger_type", "trigger_value", "pct_to_sell", "executed_at",
]

_UPSERT_EXIT_LADDER_SQL = """
INSERT INTO exit_ladder (ladder_id, asset, tranche_n, trigger_type, trigger_value, pct_to_sell, executed_at)
VALUES (:ladder_id, :asset, :tranche_n, :trigger_type, :trigger_value, :pct_to_sell, :executed_at)
ON CONFLICT(ladder_id) DO UPDATE SET
    asset = excluded.asset, tranche_n = excluded.tranche_n, trigger_type = excluded.trigger_type,
    trigger_value = excluded.trigger_value, pct_to_sell = excluded.pct_to_sell,
    executed_at = excluded.executed_at
"""


def upsert_thesis_log(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert thesis-journal entries (keyed by thesis_id). FISCAL.md §5.

    Enforces the discipline rule at the loader too: an entry **without** a non-empty
    ``falsification_criteria`` is rejected loudly (no thesis is saved without a way to be wrong).
    """
    for r in rows:
        if not str(r.get("falsification_criteria") or "").strip():
            raise ValueError(
                f"thesis_log '{r.get('thesis_id')}' has no falsification_criteria — "
                "a thesis must state what would prove it wrong (FISCAL.md §5)."
            )
    return _upsert_records(conn, _UPSERT_THESIS_SQL, THESIS_LOG_COLUMNS, rows, False, "thesis-log entries")


def upsert_exit_ladder(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert exit-ladder tranches (keyed by ladder_id). FISCAL.md §5."""
    return _upsert_records(conn, _UPSERT_EXIT_LADDER_SQL, EXIT_LADDER_COLUMNS, rows, False, "exit-ladder tranches")

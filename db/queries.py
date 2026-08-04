"""Read helpers over the observations table (CLAUDE.md sections 6-8).

The write side lives in :mod:`db.loader`; this module holds the read counterpart
used by transforms and the dashboard. Keeping reads here avoids scattering raw SQL
through the transform/UI layers.

All functions take an open sqlite3 connection and return plain Python / pandas
objects. None of them touch the network.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd


def latest_observation(
    conn: sqlite3.Connection, source: str, series_id: str
) -> tuple[str, float] | None:
    """Return ``(ts, value)`` for the most recent observation of a series, or None.

    "Most recent" is by reference date ``ts`` (ISO8601 sorts lexicographically).
    """
    row = conn.execute(
        """
        SELECT ts, value FROM observations
        WHERE source = ? AND series_id = ?
        ORDER BY ts DESC LIMIT 1
        """,
        (source, series_id),
    ).fetchone()
    if row is None or row[1] is None:
        return None
    return row[0], float(row[1])


def series_history(
    conn: sqlite3.Connection, source: str, series_id: str
) -> pd.Series:
    """Return the full history of a series as a Series indexed by ts (datetime).

    Ascending by date. Empty Series if the series is absent.
    """
    rows = conn.execute(
        """
        SELECT ts, value FROM observations
        WHERE source = ? AND series_id = ?
        ORDER BY ts ASC
        """,
        (source, series_id),
    ).fetchall()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    values = [None if r[1] is None else float(r[1]) for r in rows]
    return pd.Series(values, index=idx, dtype="float64")


def series_release_history(
    conn: sqlite3.Connection, source: str, series_id: str
) -> pd.Series:
    """Full history indexed by **release date** (``ts_release``, falling back to ``ts``).

    Point-in-time view for look-ahead-free backtests (§9): a macro reading is only *known*
    from the day it was published, so signals must be built on this index, not on ``ts``
    (the reference date, which is earlier). Duplicate release dates keep the last value.
    Ascending; empty Series if absent.
    """
    rows = conn.execute(
        """
        SELECT COALESCE(ts_release, ts) AS rel, value FROM observations
        WHERE source = ? AND series_id = ?
        ORDER BY rel ASC
        """,
        (source, series_id),
    ).fetchall()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    values = [None if r[1] is None else float(r[1]) for r in rows]
    s = pd.Series(values, index=idx, dtype="float64")
    return s[~s.index.duplicated(keep="last")]


def latest_by_source(conn: sqlite3.Connection, source: str) -> dict[str, float]:
    """Return ``{series_id: latest_value}`` for every series of a given source.

    Uses the max ts per series. Handy for one-shot snapshots (e.g. all latest
    market caps for a dominance calculation).
    """
    rows = conn.execute(
        """
        SELECT o.series_id, o.value
        FROM observations o
        JOIN (
            SELECT series_id, MAX(ts) AS mts
            FROM observations
            WHERE source = ?
            GROUP BY series_id
        ) m ON o.series_id = m.series_id AND o.ts = m.mts
        WHERE o.source = ?
        """,
        (source, source),
    ).fetchall()
    return {sid: float(val) for sid, val in rows if val is not None}


def last_ingest_at(conn: sqlite3.Connection) -> str | None:
    """Most recent ``ingested_at`` across all observations — when the last ``run_ingest`` wrote
    data. The freshness stamp for a pull-not-push panel (§2). None if the DB has no observations."""
    row = conn.execute("SELECT MAX(ingested_at) FROM observations").fetchone()
    return row[0] if row and row[0] else None


def current_balances(conn: sqlite3.Connection, source: str = "binance") -> dict[str, float]:
    """Return ``{series_id: value}`` for ``<asset>:balance:<wallet>`` series **as of the most
    recent balance-sync date only**.

    The account sync writes a full daily snapshot of every nonzero balance. When an asset is
    fully moved out (e.g. SOL staked into BNSOL), it simply stops being emitted — so its last
    nonzero balance would linger forever as a phantom holding under a plain ``MAX(ts) per series``
    read (:func:`latest_by_source`). Anchoring to the single most-recent sync date drops any asset
    absent from the latest snapshot, which is the correct "current holdings" semantics. Manual
    holdings are stored elsewhere and handled separately by the caller.
    """
    max_ts = conn.execute(
        "SELECT MAX(ts) FROM observations WHERE source = ? AND series_id LIKE '%:balance:%'",
        (source,),
    ).fetchone()[0]
    if max_ts is None:
        return {}
    rows = conn.execute(
        "SELECT series_id, value FROM observations "
        "WHERE source = ? AND series_id LIKE '%:balance:%' AND ts = ?",
        (source, max_ts),
    ).fetchall()
    return {sid: float(val) for sid, val in rows if val is not None}


def upcoming_events(
    conn: sqlite3.Connection, within_days: int = 7, categories: tuple[str, ...] | None = None
) -> list[dict]:
    """Events whose ts falls in [today, today + within_days], ascending by date.

    Reads the ``events`` table (populated by the FRED release-date ingester and any
    other calendar source). Optionally filters by category (e.g. ('macro',)).

    Returns:
        List of {category, date (ISO), label, days_until, payload}, soonest first.
    """
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=within_days)
    rows = conn.execute("SELECT category, ts, label, payload FROM events").fetchall()
    out: list[dict] = []
    for category, ts, label, payload in rows:
        if categories and category not in categories:
            continue
        try:
            date = datetime.fromisoformat(ts).date()
        except (ValueError, TypeError):
            continue
        if today <= date <= horizon:
            out.append(
                {
                    "category": category,
                    "date": date.isoformat(),
                    "label": label,
                    "days_until": (date - today).days,
                    "payload": payload,
                }
            )
    return sorted(out, key=lambda e: e["date"])

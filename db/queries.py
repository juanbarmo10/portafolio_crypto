"""Read helpers over the observations table (CLAUDE.md sections 6-8).

The write side lives in :mod:`db.loader`; this module holds the read counterpart
used by transforms and the dashboard. Keeping reads here avoids scattering raw SQL
through the transform/UI layers.

All functions take an open sqlite3 connection and return plain Python / pandas
objects. None of them touch the network.
"""

from __future__ import annotations

import sqlite3

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

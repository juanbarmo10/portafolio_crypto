"""Tests for the FRED release-date calendar (parser + events upsert/read).

Covers the events strip pipeline (Fase 5 P1) without touching the network: the
parser is pure and the loader/reader run against a temporary DB.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from db.loader import init_db, upsert_events
from db.queries import upcoming_events
from ingest.fred_releases import parse_release_dates


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = init_db(tmp_path / "t.db")
    yield connection
    connection.close()


def test_parse_release_dates_filters_and_builds() -> None:
    payload = {
        "release_dates": [
            {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-08-12"},
            {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-06-01"},  # past
            {"release_id": 10, "release_name": "Consumer Price Index", "date": "2027-01-01"},  # far
            {"release_id": 10, "date": ""},  # malformed -> skipped
        ]
    }
    today = date(2026, 8, 1)
    out = parse_release_dates(payload, release_id=10, label="CPI", today=today, horizon_days=45)

    assert len(out) == 1  # only the in-window date survives
    e = out[0]
    assert e["event_id"] == "fred:10:2026-08-12"  # deterministic -> idempotent
    assert e["category"] == "macro"
    assert e["label"] == "CPI"
    assert e["ts"] == "2026-08-12T00:00:00+00:00"
    assert json.loads(e["payload"])["release_id"] == 10


def test_upsert_events_idempotent(conn) -> None:
    df = pd.DataFrame(
        [
            {
                "event_id": "fred:10:2026-08-12",
                "category": "macro",
                "ts": "2026-08-12T00:00:00+00:00",
                "label": "CPI",
                "payload": '{"release_id": 10}',
            }
        ]
    )
    assert upsert_events(conn, df) == 1
    upsert_events(conn, df)  # re-run must not duplicate
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_upcoming_events_window_and_category(conn) -> None:
    today = datetime.now(timezone.utc).date()
    soon = (today + timedelta(days=3)).isoformat()
    far = (today + timedelta(days=20)).isoformat()
    upsert_events(
        conn,
        pd.DataFrame(
            [
                {"event_id": "a", "category": "macro", "ts": f"{soon}T00:00:00+00:00",
                 "label": "CPI", "payload": None},
                {"event_id": "b", "category": "macro", "ts": f"{far}T00:00:00+00:00",
                 "label": "NFP", "payload": None},
                {"event_id": "c", "category": "unlock", "ts": f"{soon}T00:00:00+00:00",
                 "label": "SUI", "payload": None},
            ]
        ),
    )
    within = upcoming_events(conn, within_days=7, categories=("macro",))
    labels = {e["label"] for e in within}
    assert labels == {"CPI"}  # NFP out of window, unlock filtered by category
    assert within[0]["days_until"] == 3

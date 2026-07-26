"""FRED release-date calendar ingester (CLAUDE.md sections 4, 8; Fase 5 P1).

Fetches the **upcoming** publication dates of the high-impact macro releases we track
(CPI, PCE, NFP) from FRED's free ``release/dates`` endpoint and writes them to the
``events`` table (category ``macro``). This powers the "next 7 days" strip and the
``macro_release_soon`` alert rule ("do not execute DCA today").

Only future dates matter here (the checklist asks "is there a release in the next 7
days?"), so we query with ``realtime_start = today`` and keep dates within a horizon.

Contract: :meth:`fetch` returns an empty observations frame (this source produces no
observations); :meth:`fetch_events` returns the events frame the loader upserts.
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import requests

from core.config import Settings
from core.logging_setup import get_logger
from db.loader import EVENT_COLUMNS, OBSERVATION_COLUMNS
from ingest.base import Ingester, retry

log = get_logger(__name__)

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"


def parse_release_dates(
    payload: dict, *, release_id: int, label: str, today: dt.date, horizon_days: int
) -> list[dict]:
    """Turn a FRED release/dates JSON payload into event rows (pure, testable).

    Keeps dates in ``[today, today + horizon_days]`` and builds one event per date,
    keyed deterministically by ``fred:<release_id>:<date>`` for idempotency.

    Args:
        payload: Parsed JSON from the release/dates endpoint.
        release_id: FRED release id (for the event_id and payload).
        label: Human label shown in the strip (e.g. "CPI").
        today: Reference date (UTC) for the lower bound.
        horizon_days: Keep dates up to this many days ahead.

    Returns:
        List of dicts with :data:`EVENT_COLUMNS`.
    """
    horizon = today + dt.timedelta(days=horizon_days)
    out: list[dict] = []
    for row in payload.get("release_dates", []):
        raw = row.get("date")
        if not raw:
            continue
        try:
            date = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        if not (today <= date <= horizon):
            continue
        out.append(
            {
                "event_id": f"fred:{release_id}:{raw}",
                "category": "macro",
                "ts": f"{raw}T00:00:00+00:00",
                "label": label,
                "payload": json.dumps(
                    {"release_id": release_id, "release_name": row.get("release_name"), "date": raw}
                ),
            }
        )
    return out


class FredReleasesIngester(Ingester):
    """Fetch upcoming FRED release dates for the configured releases -> events."""

    source = "fred_releases"

    def __init__(self, settings: Settings) -> None:
        """Store config and the API key.

        Raises:
            RuntimeError: If FRED_API_KEY is unset (the runner skips this ingester).
        """
        cfg = settings.source("fred")
        self._timeout = cfg.get("request_timeout_s", 20)
        self._releases: list[dict] = list(cfg.get("releases", []))
        self._horizon_days = int(cfg.get("releases_horizon_days", 45))
        api_key = settings.secrets.get("FRED_API_KEY")
        if not api_key:
            raise RuntimeError("FRED_API_KEY not set; cannot construct FredReleasesIngester.")
        self._api_key = api_key

    def fetch(self) -> pd.DataFrame:
        """This source emits no observations; return an empty (valid) frame."""
        return self.validate(pd.DataFrame(columns=OBSERVATION_COLUMNS))

    def _get(self, release_id: int, today: str) -> requests.Response:
        """One release/dates request (future dates included), retried on transient errors."""
        params = {
            "release_id": release_id,
            "api_key": self._api_key,
            "file_type": "json",
            "include_release_dates_with_no_data": "true",  # future scheduled dates
            "realtime_start": today,
            "sort_order": "asc",
            "limit": 24,
        }
        return retry(
            lambda: requests.get(FRED_RELEASE_DATES_URL, params=params, timeout=self._timeout),
            exceptions=(requests.RequestException,),
        )

    def fetch_events(self) -> pd.DataFrame:
        """Return upcoming release dates for all configured releases as events."""
        today = dt.datetime.now(dt.timezone.utc).date()
        rows: list[dict] = []
        for rel in self._releases:
            release_id = int(rel["release_id"])
            label = rel.get("label", f"release {release_id}")
            resp = self._get(release_id, today.isoformat())
            if resp.status_code != 200:
                raise RuntimeError(f"FRED release/dates {release_id}: HTTP {resp.status_code}")
            events = parse_release_dates(
                resp.json(),
                release_id=release_id,
                label=label,
                today=today,
                horizon_days=self._horizon_days,
            )
            log.info("FRED releases: %s (id %d) -> %d upcoming.", label, release_id, len(events))
            rows.extend(events)
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)

"""FRED ingester: macro series with point-in-time release dates (CLAUDE.md sections 4, 8, 9).

This is where the look-ahead-bias guard (section 9) is enforced. FRED data is
revised, and a datum's *reference* date (e.g. June CPI) precedes its *publication*
date (mid-July). We store both:

    - ts          = reference date (the month/day the datum describes)
    - ts_release  = the date the value was FIRST published (its initial release)
    - value       = the value as first published (the initial print)

Using the first release (not the latest revision) makes every stored point knowable
at ``ts_release``, so backtests that filter ``ts_release <= sim_date`` never peek
into the future.

Implementation note — why raw REST instead of ``fredapi``:
    The point-in-time data comes from FRED's ``series/observations`` endpoint with
    ``output_type=4`` ("initial release only"), which returns one row per observation
    stamped with its first-release ``realtime_start``. ``fredapi`` cannot request that
    mode, and its ``get_series_all_releases`` downloads *every* vintage, which trips
    FRED's 2000-vintage-date limit on daily series (DFF, T10Y2Y). We therefore call
    the REST endpoint directly and bisect the real-time window whenever the vintage
    limit is hit, so any series frequency works generically.

Requires FRED_API_KEY (free). If it is absent the runner skips this ingester.

fetch() -> DataFrame[source, series_id, ts, ts_release, value]
    series_id = the FRED code, e.g. "CPIAUCSL".
"""

from __future__ import annotations

import datetime as dt
import re

import pandas as pd
import requests

from core.config import Settings
from core.logging_setup import get_logger
from ingest.base import Ingester, retry

log = get_logger(__name__)

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

# FRED sentinel for a missing value in the observations endpoint.
_MISSING = "."

# Substring identifying the recoverable "too many vintage dates" 400 error.
_VINTAGE_LIMIT_MARKER = "vintage dates"

# FRED runs on US Central time, so a UTC-derived "today" can be a day ahead of
# FRED's calendar. When that happens FRED reports its own today in the 400 message;
# we parse it and clamp realtime_end to it.
_FRED_TODAY_RE = re.compile(r"today's date \((\d{4}-\d{2}-\d{2})\)")

OUTPUT_COLUMNS = ["source", "series_id", "ts", "ts_release", "value"]


def parse_initial_releases(observations: list[dict], series_id: str) -> pd.DataFrame:
    """Convert FRED ``output_type=4`` observations to the long loader contract.

    Pure function (no network) so it is unit-testable without an API key.

    Args:
        observations: List of ``{"date", "realtime_start", "value"}`` dicts as
            returned by the FRED observations endpoint with ``output_type=4``.
        series_id: FRED code to stamp on the output (e.g. "CPIAUCSL").

    Returns:
        Long DataFrame ``[source, series_id, ts, ts_release, value]``, one row per
        reference date, keeping the earliest release when duplicates appear (a
        safety net for bisected windows). Rows with a missing value are dropped.
    """
    # Keep the earliest realtime_start per reference date.
    best: dict[str, dict] = {}
    for obs in observations:
        value = obs.get("value")
        if value is None or value == _MISSING:
            continue
        ref = obs["date"]
        if ref not in best or obs["realtime_start"] < best[ref]["realtime_start"]:
            best[ref] = obs

    if not best:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    refs = sorted(best)
    out = pd.DataFrame(
        {
            "source": "fred",
            "series_id": series_id,
            "ts": [f"{r}T00:00:00+00:00" for r in refs],
            "ts_release": [f"{best[r]['realtime_start']}T00:00:00+00:00" for r in refs],
            "value": [float(best[r]["value"]) for r in refs],
        }
    )
    return out


class FredIngester(Ingester):
    """Fetch configured FRED macro series with point-in-time release dates."""

    source = "fred"

    def __init__(self, settings: Settings) -> None:
        """Store config and the API key.

        Raises:
            RuntimeError: If FRED_API_KEY is not set (the runner should skip this
                ingester rather than construct it without a key).
        """
        self._settings = settings
        cfg = settings.source("fred")
        self._timeout = cfg.get("request_timeout_s", 20)
        self._observation_start = cfg.get("observation_start", "2000-01-01")
        self._series: dict[str, str] = dict(cfg.get("series", {}))

        api_key = settings.secrets.get("FRED_API_KEY")
        if not api_key:
            raise RuntimeError("FRED_API_KEY not set; cannot construct FredIngester.")
        self._api_key = api_key

    def _get(self, code: str, realtime_start: str, realtime_end: str) -> requests.Response:
        """One observations request (output_type=4), retried on transient errors."""
        params = {
            "series_id": code,
            "api_key": self._api_key,
            "file_type": "json",
            "observation_start": self._observation_start,
            "realtime_start": realtime_start,
            "realtime_end": realtime_end,
            "output_type": 4,  # initial release only -> one row per observation
        }
        return retry(
            lambda: requests.get(FRED_OBSERVATIONS_URL, params=params, timeout=self._timeout),
            exceptions=(requests.RequestException,),
        )

    def _fetch_observations(
        self, code: str, realtime_start: str, realtime_end: str
    ) -> list[dict]:
        """Fetch initial-release observations, bisecting the real-time window if needed.

        FRED caps a request at 2000 vintage dates; daily series exceed that over a
        multi-year window. On that specific 400 we split the real-time window in half
        and recurse. Each initial release falls in exactly one half, so the union is
        complete.
        """
        resp = self._get(code, realtime_start, realtime_end)
        if resp.status_code == 200:
            return resp.json().get("observations", [])

        message = resp.json().get("error_message", "") if resp.content else ""
        if resp.status_code == 400:
            # Clock skew: realtime_end is past FRED's today -> clamp to it and retry.
            match = _FRED_TODAY_RE.search(message)
            if match and realtime_end > match.group(1):
                return self._fetch_observations(code, realtime_start, match.group(1))

            # Daily series exceed the 2000-vintage-date limit -> bisect the window.
            if _VINTAGE_LIMIT_MARKER in message:
                start = dt.date.fromisoformat(realtime_start)
                end = dt.date.fromisoformat(realtime_end)
                if start >= end:
                    raise RuntimeError(f"FRED {code}: vintage limit on a single day {start}.")
                mid = start + (end - start) // 2
                left = self._fetch_observations(code, realtime_start, mid.isoformat())
                right = self._fetch_observations(
                    code, (mid + dt.timedelta(days=1)).isoformat(), realtime_end
                )
                return left + right

        raise RuntimeError(f"FRED {code}: HTTP {resp.status_code} {message[:120]}")

    def fetch(self) -> pd.DataFrame:
        """Return all configured macro series as a long observations DataFrame."""
        realtime_end = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        frames: list[pd.DataFrame] = []
        for name, code in self._series.items():
            observations = self._fetch_observations(code, self._observation_start, realtime_end)
            reduced = parse_initial_releases(observations, code)
            log.info("FRED: %s (%s) -> %d points.", name, code, len(reduced))
            frames.append(reduced)

        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=OUTPUT_COLUMNS)
        return self.validate(df)

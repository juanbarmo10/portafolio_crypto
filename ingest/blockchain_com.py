"""Blockchain.com on-chain charts ingester (CLAUDE.md sections 4, 8; IDEAS_MEJORAS A10).

Free, keyless BTC network fundamentals — hash rate, difficulty, transaction count,
active addresses, miners' revenue. These map to checklist level 3 (is BTC's thesis —
network security and real usage — intact?) and are a **partial** free substitute for the
paid on-chain metrics (Glassnode) that section 4 puts out of scope. They are NOT MVRV /
SOPR / holder cohorts (those stay out of scope; this is not a backdoor to them).

series_id ``btc:<chart>``, e.g. ``btc:hash-rate``, ``btc:n-transactions``.

fetch() -> DataFrame[source, series_id, ts, ts_release, value]
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from core.config import Settings
from core.logging_setup import get_logger
from ingest.base import Ingester, retry

log = get_logger(__name__)


def parse_bcom_chart(payload: dict, chart: str) -> list[dict[str, Any]]:
    """Blockchain.com ``/charts/<chart>`` response -> daily observation rows (pure).

    ``values`` is ``[{"x": unix_seconds, "y": value}, ...]``. Dedup by UTC day (last
    value wins) so a re-run is idempotent. series_id ``btc:<chart>``. Fails loudly if
    the ``values`` list disappears (§9).
    """
    by_day: dict[str, float] = {}
    for point in payload["values"]:
        x, y = point.get("x"), point.get("y")
        if x is None or y is None:
            continue
        try:
            day = datetime.fromtimestamp(int(x), tz=timezone.utc).strftime("%Y-%m-%d")
            by_day[day] = float(y)
        except (TypeError, ValueError):
            continue
    return [
        {
            "source": "blockchain_com",
            "series_id": f"btc:{chart}",
            "ts": f"{day}T00:00:00+00:00",
            "ts_release": None,
            "value": value,
        }
        for day, value in sorted(by_day.items())
    ]


class BlockchainComIngester(Ingester):
    """Fetch configured BTC on-chain charts from Blockchain.com (keyless, daily)."""

    source = "blockchain_com"

    def __init__(self, settings: Settings) -> None:
        cfg = settings.source("blockchain_com")
        self._base_url = cfg.get("base_url", "https://api.blockchain.info/charts")
        self._charts: list[str] = cfg.get(
            "charts", ["hash-rate", "difficulty", "n-transactions"]
        )
        self._timespan = cfg.get("timespan", "1year")
        self._timeout = cfg.get("request_timeout_s", 25)

    def _fetch_chart(self, chart: str) -> dict:
        resp = requests.get(
            f"{self._base_url}/{chart}",
            params={"format": "json", "timespan": self._timespan},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> pd.DataFrame:
        """Return all configured BTC on-chain charts as a long observations DataFrame."""
        rows: list[dict[str, Any]] = []
        for chart in self._charts:
            try:
                payload = retry(
                    lambda c=chart: self._fetch_chart(c),
                    exceptions=(requests.RequestException,),
                )
            except requests.RequestException:
                log.warning("Blockchain.com: chart %s failed; skipping.", chart)
                continue
            parsed = parse_bcom_chart(payload, chart)
            log.info("Blockchain.com: %s -> %d daily points.", chart, len(parsed))
            rows.extend(parsed)
        df = pd.DataFrame(rows, columns=["source", "series_id", "ts", "ts_release", "value"])
        return self.validate(df)

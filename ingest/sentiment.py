"""Sentiment ingester: Crypto Fear & Greed Index (CLAUDE.md sections 4, 8; IDEAS_MEJORAS A6).

Alternative.me publishes a free, keyless, daily Fear & Greed Index (a composite of
volatility, momentum, volume, dominance and social signals). Used **contrarian and as
weekly context** — extreme fear can mark local bottoms, extreme greed local tops (§2
levels 1/2). Design note (§2, §11): a fear/greed gauge is exactly the kind of widget that
invites over-trading, so the dashboard presents it as a quiet weekly caption, never
highlighted or animated.

series_id ``sentiment:fear_greed`` (0 = extreme fear, 100 = extreme greed).

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


def parse_fear_greed(payload: dict) -> list[dict[str, Any]]:
    """Alternative.me ``/fng/`` response -> daily Fear & Greed observation rows (pure).

    ``data`` is ``[{"value": "30", "value_classification": "Fear", "timestamp": "..."}]``
    with string fields and a unix-seconds timestamp. Dedup by UTC day. Fails loudly if
    the ``data`` list disappears (§9).
    """
    by_day: dict[str, float] = {}
    for item in payload["data"]:
        value = item.get("value")
        ts = item.get("timestamp")
        if value is None or ts is None:
            continue
        try:
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            by_day[day] = float(value)
        except (TypeError, ValueError):
            continue
    return [
        {
            "source": "alternative_me",
            "series_id": "sentiment:fear_greed",
            "ts": f"{day}T00:00:00+00:00",
            "ts_release": None,
            "value": value,
        }
        for day, value in sorted(by_day.items())
    ]


class FearGreedIngester(Ingester):
    """Fetch the Crypto Fear & Greed Index history (keyless, daily)."""

    source = "alternative_me"

    def __init__(self, settings: Settings) -> None:
        cfg = settings.source("fear_greed")
        self._base_url = cfg.get("base_url", "https://api.alternative.me/fng/")
        self._history_days = int(cfg.get("history_days", 120))
        self._timeout = cfg.get("request_timeout_s", 20)

    def _fetch(self) -> dict:
        resp = requests.get(
            self._base_url,
            params={"limit": self._history_days, "format": "json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> pd.DataFrame:
        """Return the Fear & Greed daily history as a long observations DataFrame."""
        try:
            payload = retry(self._fetch, exceptions=(requests.RequestException,))
        except requests.RequestException:
            log.warning("Fear & Greed: request failed; skipping.")
            return self.validate(
                pd.DataFrame(columns=["source", "series_id", "ts", "ts_release", "value"])
            )
        rows = parse_fear_greed(payload)
        log.info("Fear & Greed: %d daily points.", len(rows))
        df = pd.DataFrame(rows, columns=["source", "series_id", "ts", "ts_release", "value"])
        return self.validate(df)

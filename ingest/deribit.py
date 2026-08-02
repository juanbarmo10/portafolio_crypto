"""Deribit ingester: DVOL implied-volatility index (CLAUDE.md sections 4, 8; RESEARCH.md A5).

Deribit's public API needs no key. DVOL is the options market's implied-volatility index
for BTC/ETH — the "VIX of crypto" — and adds what perpetual funding/OI cannot see: what
the options market is paying for volatility (fear vs. complacency, checklist level 2).

Daily snapshot only (``resolution=86400``): a single close per UTC day, consistent with
the anti-intraday design (§2, §11). series_id ``deribit:dvol:<currency>`` (lowercased).

fetch() -> DataFrame[source, series_id, ts, ts_release, value]

Note (scope): 25-delta skew (put vs. call demand) is a natural follow-up but needs the
full options chain (interpolating IV by delta per expiry), which is materially more
fragile than the index. DVOL — the clean, single-endpoint signal — ships here; skew is
deliberately deferred rather than shipped half-built (§9: fail loudly, not silently wrong).
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


def parse_dvol(payload: dict, currency: str) -> list[dict[str, Any]]:
    """Deribit ``get_volatility_index_data`` response -> daily DVOL observation rows (pure).

    ``result.data`` is ``[[ts_ms, open, high, low, close], ...]``; we keep the **close**
    per UTC day (last write wins), so a re-run is idempotent on ``(series_id, ts)``.
    series_id ``deribit:dvol:<currency>`` (currency lowercased). Fails loudly if the
    envelope shape (``result.data``) disappears (§9).
    """
    data = payload["result"]["data"]
    by_day: dict[str, float] = {}
    for row in data:
        if not row or len(row) < 5 or row[4] is None:
            continue
        ts_ms, close = row[0], row[4]
        day = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = float(close)
    return [
        {
            "source": "deribit",
            "series_id": f"deribit:dvol:{currency.lower()}",
            "ts": f"{day}T00:00:00+00:00",
            "ts_release": None,
            "value": value,
        }
        for day, value in sorted(by_day.items())
    ]


class DeribitIngester(Ingester):
    """Fetch the DVOL implied-volatility index for the configured currencies (BTC/ETH)."""

    source = "deribit"

    def __init__(self, settings: Settings) -> None:
        cfg = settings.source("deribit")
        self._base_url = cfg.get("base_url", "https://www.deribit.com/api/v2")
        self._currencies: list[str] = cfg.get("currencies", ["BTC", "ETH"])
        self._history_days = int(cfg.get("dvol_history_days", 90))
        self._timeout = cfg.get("request_timeout_s", 20)

    def _fetch_currency(self, currency: str, end_ms: int) -> dict:
        """One daily-resolution DVOL request for a currency."""
        params = {
            "currency": currency,
            "start_timestamp": end_ms - self._history_days * 86_400_000,
            "end_timestamp": end_ms,
            "resolution": "86400",  # daily
        }
        resp = requests.get(
            f"{self._base_url}/public/get_volatility_index_data",
            params=params,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> pd.DataFrame:
        """Return DVOL daily history for all configured currencies as a long DataFrame."""
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        rows: list[dict[str, Any]] = []
        for currency in self._currencies:
            try:
                payload = retry(
                    lambda c=currency: self._fetch_currency(c, end_ms),
                    exceptions=(requests.RequestException,),
                )
            except requests.RequestException:
                log.warning("Deribit DVOL: %s failed; skipping.", currency)
                continue
            parsed = parse_dvol(payload, currency)
            log.info("Deribit DVOL: %s -> %d daily points.", currency, len(parsed))
            rows.extend(parsed)
        df = pd.DataFrame(rows, columns=["source", "series_id", "ts", "ts_release", "value"])
        return self.validate(df)

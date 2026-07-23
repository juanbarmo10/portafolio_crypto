"""CoinGecko ingester: market snapshot for the tracked assets (CLAUDE.md sections 4, 8).

Uses the batch ``/coins/markets`` endpoint (one request for all assets) to respect
the free-tier rate limit (~30 req/min, section 9: "no hacer una llamada por activo
si existe endpoint batch"). Emits one observation per (asset, metric) for the
current UTC day.

Design note — idempotency (section 6/8): the reference timestamp ``ts`` is
truncated to the current UTC *date*, so re-running on the same day upserts the
same primary keys instead of creating new rows. This also matches the daily-data
preference (section 2). ``ts_release`` is null: market data is real-time, so its
reference date is its publication date.

fetch() -> DataFrame[source, series_id, ts, ts_release, value]
    series_id = "<coingecko_id>:<metric>", e.g. "ethereum:price".
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

# Map our series metric name -> key in the /coins/markets response.
_METRIC_KEYS: dict[str, str] = {
    "price": "current_price",
    "market_cap": "market_cap",
    "volume_24h": "total_volume",
    "circulating_supply": "circulating_supply",
    "total_supply": "total_supply",
    "max_supply": "max_supply",
    "ath": "ath",
}


class CoinGeckoIngester(Ingester):
    """Fetch a daily market snapshot for all configured assets."""

    source = "coingecko"

    def __init__(self, settings: Settings) -> None:
        """Store config; read the coingecko source block and asset list."""
        self._settings = settings
        cfg = settings.source("coingecko")
        self._base_url = cfg["base_url"]
        self._vs_currency = cfg.get("vs_currency", "usd")
        self._timeout = cfg.get("request_timeout_s", 20)
        self._ids = [a["coingecko_id"] for a in settings.assets]

    def _get_markets(self) -> list[dict[str, Any]]:
        """Call /coins/markets for all asset ids in one batched request."""
        params = {
            "vs_currency": self._vs_currency,
            "ids": ",".join(self._ids),
            "order": "market_cap_desc",
            "per_page": len(self._ids),
            "page": 1,
            "sparkline": "false",
        }
        resp = requests.get(
            f"{self._base_url}/coins/markets", params=params, timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()

    def _get_global(self) -> dict[str, Any]:
        """Call /global for whole-market aggregates (true BTC/ETH dominance)."""
        resp = requests.get(f"{self._base_url}/global", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def fetch(self) -> pd.DataFrame:
        """Return the daily snapshot as a long observations DataFrame."""
        data = retry(
            self._get_markets,
            exceptions=(requests.RequestException,),
        )
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")

        rows: list[dict[str, Any]] = []
        for coin in data:
            coin_id = coin.get("id")
            for metric, key in _METRIC_KEYS.items():
                value = coin.get(key)
                if value is None:  # e.g. max_supply is null for uncapped tokens
                    continue
                rows.append(
                    {
                        "source": self.source,
                        "series_id": f"{coin_id}:{metric}",
                        "ts": ts,
                        "ts_release": None,
                        "value": value,
                    }
                )

        # Whole-market aggregates: true dominance (BTC share of ALL crypto mcap,
        # not just the tracked universe) and total market cap. One extra request.
        glob = retry(self._get_global, exceptions=(requests.RequestException,))
        mcap_pct = glob.get("market_cap_percentage", {})
        total_mcap = glob.get("total_market_cap", {}).get(self._vs_currency)
        global_series = {
            "global:btc_dominance": mcap_pct.get("btc"),
            "global:eth_dominance": mcap_pct.get("eth"),
            "global:total_market_cap": total_mcap,
        }
        for series_id, value in global_series.items():
            if value is None:
                continue
            rows.append(
                {
                    "source": self.source,
                    "series_id": series_id,
                    "ts": ts,
                    "ts_release": None,
                    "value": value,
                }
            )

        df = pd.DataFrame(rows, columns=["source", "series_id", "ts", "ts_release", "value"])
        log.info("CoinGecko: %d observations across %d assets (+ global).", len(df), len(data))
        return self.validate(df)

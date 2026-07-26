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

import time
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


def parse_market_chart(payload: dict, coin_id: str) -> list[dict]:
    """Turn a /coins/{id}/market_chart response into daily price observations.

    ``prices`` is ``[[ms_timestamp, price], ...]``; for a >90-day window CoinGecko
    returns one point per day (plus a trailing intraday "now"). Dedup by UTC date and
    stamp ts at day midnight so backfilled points merge with the daily snapshot
    (idempotent upsert). Pure/testable.
    """
    by_day: dict[str, float] = {}
    for point in payload.get("prices", []):
        if not point or point[1] is None:
            continue
        day = datetime.fromtimestamp(point[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = float(point[1])  # last price seen for the day wins
    return [
        {
            "source": "coingecko",
            "series_id": f"{coin_id}:price",
            "ts": f"{day}T00:00:00+00:00",
            "ts_release": None,
            "value": price,
        }
        for day, price in sorted(by_day.items())
    ]


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
        # Tracked assets + any price-alias ids (e.g. wrapped-beacon-eth) used to value
        # held-but-untracked assets like WBETH in the level-4 holdings view.
        alias_ids = [
            cid
            for cid in (settings.source("binance_account").get("price_aliases", {}) or {}).values()
            if cid
        ]
        self._ids = list(dict.fromkeys([a["coingecko_id"] for a in settings.assets] + alias_ids))

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

    def fetch_history(self, days: int = 365) -> pd.DataFrame:
        """One-time backfill of daily prices per asset via /coins/{id}/market_chart.

        The daily snapshot only accrues going forward; this catches up history so
        change/return/baseline calcs (24h/7d/30d, DCA-vs-baseline) have real data.
        One request per asset, paced to stay under the free ~30 req/min limit.
        Idempotent (same day -> ON CONFLICT overwrite). Invoked via ``run_ingest.py
        --backfill``, not the daily run.
        """
        sleep_s = float(self._settings.source("coingecko").get("history_sleep_s", 6))
        rows: list[dict[str, Any]] = []
        for coin_id in self._ids:
            def _call(cid: str = coin_id) -> dict:
                resp = requests.get(
                    f"{self._base_url}/coins/{cid}/market_chart",
                    params={"vs_currency": self._vs_currency, "days": days},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                return resp.json()

            try:
                payload = retry(
                    _call, attempts=4, base_delay_s=5, max_delay_s=30,
                    exceptions=(requests.RequestException,),
                )
            except requests.RequestException as exc:
                # Free-tier rate limit: skip this asset (idempotent re-run fills it) rather
                # than aborting the whole backfill.
                log.warning("CoinGecko history: %s failed (%s); skipping.", coin_id, exc)
                time.sleep(sleep_s)
                continue
            parsed = parse_market_chart(payload, coin_id)
            log.info("CoinGecko history: %s -> %d daily points.", coin_id, len(parsed))
            rows.extend(parsed)
            time.sleep(sleep_s)  # pace requests under the free rate limit
        return self.validate(
            pd.DataFrame(rows, columns=["source", "series_id", "ts", "ts_release", "value"])
        )

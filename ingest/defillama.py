"""DefiLlama ingester: historical TVL for protocols and chains (CLAUDE.md sections 4, 8).

For each asset that declares a ``defillama`` block in settings.yaml this fetches the
*historical* TVL series (not just the current point) so weekly/monthly TVL variation
is available immediately from stored data:

    - kind: protocol -> GET /protocol/<slug>      (uses the top-level 'tvl' history)
    - kind: chain    -> GET /v2/historicalChainTvl/<slug>

Only the most recent ``history_days`` points are kept (config), which is enough for
7/30/90-day indicators while bounding database size. Storing per-date points is
idempotent: each (source, series_id, ts) is a primary key.

fetch() -> DataFrame[source, series_id, ts, ts_release, value]
    series_id = "<slug>:tvl", e.g. "aave:tvl" or "Sui:tvl". ts_release is null
    (TVL is measured, not released on a schedule).
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


class DefiLlamaIngester(Ingester):
    """Fetch historical TVL for every asset with a defillama block in config."""

    source = "defillama"

    def __init__(self, settings: Settings) -> None:
        """Store config; collect (kind, slug) targets from the asset list."""
        self._settings = settings
        cfg = settings.source("defillama")
        self._base_url = cfg["base_url"]
        self._timeout = cfg.get("request_timeout_s", 20)
        self._history_days = int(cfg.get("history_days", 400))
        self._targets = [
            (a["defillama"]["kind"], a["defillama"]["slug"])
            for a in settings.assets
            if "defillama" in a
        ]

    def _fetch_protocol(self, slug: str) -> list[tuple[str, float]]:
        """Return [(date_iso, tvl)] history for a protocol slug."""
        resp = requests.get(f"{self._base_url}/protocol/{slug}", timeout=self._timeout)
        resp.raise_for_status()
        series = resp.json().get("tvl", [])
        return [
            (self._unix_to_iso(pt["date"]), pt["totalLiquidityUSD"])
            for pt in series
            if pt.get("totalLiquidityUSD") is not None
        ]

    def _fetch_chain(self, slug: str) -> list[tuple[str, float]]:
        """Return [(date_iso, tvl)] history for a chain name."""
        resp = requests.get(
            f"{self._base_url}/v2/historicalChainTvl/{slug}", timeout=self._timeout
        )
        resp.raise_for_status()
        series = resp.json()
        return [
            (self._unix_to_iso(pt["date"]), pt["tvl"])
            for pt in series
            if pt.get("tvl") is not None
        ]

    @staticmethod
    def _unix_to_iso(unix_ts: int | str) -> str:
        """Convert a unix seconds timestamp to an ISO8601 UTC date string."""
        dt = datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT00:00:00+00:00")

    def fetch(self) -> pd.DataFrame:
        """Return historical TVL for all targets as a long observations DataFrame."""
        rows: list[dict[str, Any]] = []
        for kind, slug in self._targets:
            fetch_one = self._fetch_protocol if kind == "protocol" else self._fetch_chain
            history = retry(
                lambda fn=fetch_one, s=slug: fn(s),
                exceptions=(requests.RequestException,),
            )
            # Keep only the most recent history_days points.
            history = history[-self._history_days :]
            for ts, value in history:
                rows.append(
                    {
                        "source": self.source,
                        "series_id": f"{slug}:tvl",
                        "ts": ts,
                        "ts_release": None,
                        "value": value,
                    }
                )
            log.info("DefiLlama: %s '%s' -> %d points.", kind, slug, len(history))

        df = pd.DataFrame(rows, columns=["source", "series_id", "ts", "ts_release", "value"])
        return self.validate(df)

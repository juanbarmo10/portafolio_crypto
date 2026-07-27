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


def _unix_to_iso_day(unix_ts: int | str) -> str:
    """Convert a unix seconds timestamp to an ISO8601 UTC date string at midnight."""
    return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")


def parse_stablecoins_chart(payload: list[dict], history_days: int) -> list[dict]:
    """Aggregate stablecoin market cap (USD) history -> observation rows (pure, A7).

    Uses ``totalCirculatingUSD.peggedUSD`` — the USD value of USD-pegged stablecoins,
    a proxy for "dry powder" (capital parked ready to enter crypto). series_id
    ``stablecoins:total_mcap``. Keeps the most recent ``history_days`` points. Fails
    loudly (AttributeError) if the top-level shape stops being a list (§9).
    """
    rows: list[dict] = []
    for pt in payload:
        usd = (pt.get("totalCirculatingUSD") or {}).get("peggedUSD")
        date = pt.get("date")
        if usd is None or date is None:
            continue
        try:
            ts = _unix_to_iso_day(date)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "source": "defillama",
                "series_id": "stablecoins:total_mcap",
                "ts": ts,
                "ts_release": None,
                "value": float(usd),
            }
        )
    rows.sort(key=lambda r: r["ts"])
    return rows[-history_days:]


def parse_revenue_history(payload: dict, slug: str, history_days: int) -> list[dict]:
    """Daily protocol **revenue** history -> observation rows (pure, A11).

    Turns the fees-summary ``totalDataChart`` (``[[unix_seconds, revenue_usd], ...]``)
    into a stored series ``<slug>:revenue`` so value accrual reads as a *trend*, not a
    snapshot (MC/Revenue over time = crypto P/E). Keeps the most recent ``history_days``.
    """
    chart = payload.get("totalDataChart") or []
    rows: list[dict] = []
    for point in chart:
        if not point or len(point) < 2 or point[1] is None:
            continue
        try:
            ts = _unix_to_iso_day(point[0])
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "source": "defillama",
                "series_id": f"{slug}:revenue",
                "ts": ts,
                "ts_release": None,
                "value": float(point[1]),
            }
        )
    rows.sort(key=lambda r: r["ts"])
    return rows[-history_days:]


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
        self._stablecoins_base_url = cfg.get("stablecoins_base_url", "https://stablecoins.llama.fi")
        self._revenue_history_days = int(cfg.get("revenue_history_days", 400))
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

    def _fetch_revenue_summary(self, slug: str) -> dict | None:
        """Full fees/revenue summary for a slug, or None if unavailable (404).

        One request gives both the 30-day snapshot (``total30d``) and the daily history
        (``totalDataChart``), so A11 (revenue as a series) adds no extra requests over
        the existing snapshot. Revenue (not total fees) is the value-accrual signal: a
        protocol can have big fees yet ~0 revenue to the token without a fee switch.
        """
        resp = requests.get(
            f"{self._base_url}/summary/fees/{slug}",
            params={"dataType": "dailyRevenue"},
            timeout=self._timeout,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _fetch_stablecoins_mcap(self) -> list[dict]:
        """Aggregate stablecoin market-cap history rows (A7). Isolated in fetch()."""
        resp = requests.get(
            f"{self._stablecoins_base_url}/stablecoincharts/all", timeout=self._timeout
        )
        resp.raise_for_status()
        return parse_stablecoins_chart(resp.json(), self._history_days)

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

        # Protocol revenue: 30d snapshot (value accrual to the token) + daily history
        # (A11). One request per slug gives both. Only protocol slugs (chains have no
        # /summary/fees entry). Isolated per-slug.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
        for kind, slug in self._targets:
            if kind != "protocol":
                continue
            try:
                summary = retry(
                    lambda s=slug: self._fetch_revenue_summary(s),
                    exceptions=(requests.RequestException,),
                )
            except requests.RequestException:
                log.warning("DefiLlama revenue: %s failed; skipping.", slug)
                continue
            if summary is None:
                continue
            revenue_30d = summary.get("total30d")
            if revenue_30d is not None:
                rows.append(
                    {
                        "source": self.source,
                        "series_id": f"{slug}:revenue_30d",
                        "ts": today,
                        "ts_release": None,
                        "value": revenue_30d,
                    }
                )
            history = parse_revenue_history(summary, slug, self._revenue_history_days)
            rows.extend(history)
            log.info("DefiLlama revenue: %s 30d=%s, %d history points.",
                     slug, revenue_30d, len(history))

        # A7: aggregate stablecoin market cap ("dry powder"). Isolated; a failure here
        # never sinks the TVL/revenue rows already collected.
        try:
            stables = retry(self._fetch_stablecoins_mcap, exceptions=(requests.RequestException,))
            rows.extend(stables)
            log.info("DefiLlama stablecoins: %d points.", len(stables))
        except requests.RequestException:
            log.warning("DefiLlama stablecoins failed; skipping.")

        df = pd.DataFrame(rows, columns=["source", "series_id", "ts", "ts_release", "value"])
        return self.validate(df)

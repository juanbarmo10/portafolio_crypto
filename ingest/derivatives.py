"""Derivatives ingester: funding rates + open interest via ccxt (CLAUDE.md sections 4, 8).

Covers checklist level 2 (§2): is a rally backed by new money or mechanical? For each
tracked asset with a perpetual market it fetches, per exchange:

    - Funding-rate history (8h cadence, ~90 days) -> enables a 90-day z-score that
      flags crowded positioning (§8: z>2 crowded longs, z<-2 crowded shorts).
    - Open-interest history (daily, ~30 days, USD notional) -> enables the price/OI
      divergence detector (price up + OI down = mechanical short-covering, fragile).

Multi-exchange (Binance + Bybit by default). Data is stored per exchange; the
transform layer (transform/rally_quality.py) aggregates. Assets without a perp market
on an exchange are skipped (logged), and any single exchange/asset failure is isolated
so it never aborts the run.

Also stores the perp's daily close (OHLCV) so the price/OI divergence is available
immediately and measured on the same venue as OI.

fetch() -> DataFrame[source, series_id, ts, ts_release, value]
    series_id = "<SYMBOL>:funding:<exchange>" | "<SYMBOL>:oi:<exchange>"
              | "<SYMBOL>:close:<exchange>"
    e.g. "BTC:funding:binance", "ETH:oi:bybit", "SOL:close:binance".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import ccxt
import pandas as pd

from core.config import Settings
from core.logging_setup import get_logger
from db.loader import OBSERVATION_COLUMNS
from ingest.base import Ingester, retry

log = get_logger(__name__)


def _ms_to_iso(ms: int) -> str:
    """Convert a millisecond epoch to an ISO8601 UTC timestamp."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _ms_to_iso_day(ms: int) -> str:
    """Convert a millisecond epoch to an ISO8601 UTC date at midnight (daily bucket)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")


class DerivativesIngester(Ingester):
    """Fetch funding-rate and open-interest history for perp markets via ccxt."""

    source = "derivatives"

    def __init__(self, settings: Settings) -> None:
        """Read the derivatives config block and the tracked-asset symbols."""
        cfg = settings.source("derivatives")
        self._exchange_ids: list[str] = cfg.get("exchanges", ["binance", "bybit"])
        self._quote: str = cfg.get("quote", "USDT")
        self._funding_days: int = int(cfg.get("funding_history_days", 90))
        self._oi_days: int = int(cfg.get("oi_history_days", 30))
        self._timeout: int = int(cfg.get("request_timeout_ms", 20000))
        self._symbols: list[str] = [a["symbol"] for a in settings.assets]

    def _make_exchange(self, exchange_id: str) -> ccxt.Exchange:
        """Instantiate a ccxt exchange with markets loaded (so symbol checks are cheap)."""
        exchange = getattr(ccxt, exchange_id)(
            {"enableRateLimit": True, "timeout": self._timeout}
        )
        exchange.load_markets()
        return exchange

    def _funding_rows(
        self, exchange: ccxt.Exchange, exchange_id: str, symbol: str, market: str
    ) -> list[dict[str, Any]]:
        """Funding-rate history rows for one (exchange, asset). Empty if unsupported."""
        if not exchange.has.get("fetchFundingRateHistory"):
            return []
        since = exchange.milliseconds() - self._funding_days * 24 * 3600 * 1000
        history = retry(
            lambda: exchange.fetch_funding_rate_history(market, since=since, limit=1000),
            exceptions=(ccxt.NetworkError,),
        )
        rows = []
        for entry in history:
            rate = entry.get("fundingRate")
            ts = entry.get("timestamp")
            if rate is None or ts is None:
                continue
            rows.append(
                {
                    "source": self.source,
                    "series_id": f"{symbol}:funding:{exchange_id}",
                    "ts": _ms_to_iso(ts),
                    "ts_release": None,
                    "value": float(rate),
                }
            )
        return rows

    def _price_rows(
        self, exchange: ccxt.Exchange, exchange_id: str, symbol: str, market: str
    ) -> list[dict[str, Any]]:
        """Daily perp close-price rows for one (exchange, asset).

        Backfilled from OHLCV so the price/OI divergence works immediately and from the
        same venue as the OI series (rather than waiting for CoinGecko price history).
        """
        if not exchange.has.get("fetchOHLCV"):
            return []
        candles = retry(
            lambda: exchange.fetch_ohlcv(market, timeframe="1d", limit=self._oi_days),
            exceptions=(ccxt.NetworkError,),
        )
        rows = []
        for ts, _open, _high, _low, close, _vol in candles:
            if close is None or ts is None:
                continue
            rows.append(
                {
                    "source": self.source,
                    "series_id": f"{symbol}:close:{exchange_id}",
                    "ts": _ms_to_iso_day(ts),
                    "ts_release": None,
                    "value": float(close),
                }
            )
        return rows

    def _oi_rows(
        self, exchange: ccxt.Exchange, exchange_id: str, symbol: str, market: str
    ) -> list[dict[str, Any]]:
        """Open-interest history rows (USD notional) for one (exchange, asset).

        Only points that expose a USD ``openInterestValue`` are stored, so OI is
        comparable across assets. Exchanges that report only base amount (e.g. Bybit)
        contribute nothing here — funding still comes through.
        """
        if not exchange.has.get("fetchOpenInterestHistory"):
            return []
        history = retry(
            lambda: exchange.fetch_open_interest_history(market, timeframe="1d", limit=self._oi_days),
            exceptions=(ccxt.NetworkError,),
        )
        rows = []
        for entry in history:
            value = entry.get("openInterestValue")  # USD notional
            ts = entry.get("timestamp")
            if value is None or ts is None:
                continue
            rows.append(
                {
                    "source": self.source,
                    "series_id": f"{symbol}:oi:{exchange_id}",
                    "ts": _ms_to_iso_day(ts),
                    "ts_release": None,
                    "value": float(value),
                }
            )
        return rows

    def fetch(self) -> pd.DataFrame:
        """Return funding + OI history across exchanges as a long observations DataFrame."""
        rows: list[dict[str, Any]] = []
        for exchange_id in self._exchange_ids:
            try:
                exchange = retry(
                    lambda eid=exchange_id: self._make_exchange(eid),
                    exceptions=(ccxt.NetworkError,),
                )
            except Exception:  # noqa: BLE001 — isolate a dead exchange, keep the rest
                log.exception("derivatives: could not initialize exchange %s", exchange_id)
                continue

            for symbol in self._symbols:
                market = f"{symbol}/{self._quote}:{self._quote}"
                if market not in exchange.markets:
                    log.debug("derivatives: %s has no %s market; skipping", exchange_id, market)
                    continue
                try:
                    rows.extend(self._funding_rows(exchange, exchange_id, symbol, market))
                except Exception:  # noqa: BLE001 — per (exchange, asset) isolation
                    log.exception("derivatives: funding %s %s failed", exchange_id, market)
                try:
                    rows.extend(self._oi_rows(exchange, exchange_id, symbol, market))
                except Exception:  # noqa: BLE001
                    log.exception("derivatives: OI %s %s failed", exchange_id, market)
                try:
                    rows.extend(self._price_rows(exchange, exchange_id, symbol, market))
                except Exception:  # noqa: BLE001
                    log.exception("derivatives: close %s %s failed", exchange_id, market)

            log.info("derivatives: %s done (%d rows so far).", exchange_id, len(rows))

        df = pd.DataFrame(rows, columns=OBSERVATION_COLUMNS)
        return self.validate(df)

"""Multi-exchange spot prices for the Coinbase premium (RESEARCH.md A8; CLAUDE.md 4, 8).

The **Coinbase premium** = (Coinbase price − Binance price) / Binance price. Positive and
rising = US spot demand (ETFs / treasuries buy on Coinbase) — real, unleveraged conviction,
complementing the leverage-driven funding/OI signals (checklist level 2).

We store the daily close for BTC/ETH on each exchange via ccxt (public, keyless); the
premium itself is a pure transform over these two series (transform/indicators). Coinbase
quotes USD, Binance quotes USDT — the standard definition tolerates the ~1.00 USDT peg, and
the dashboard labels it as such. Daily candles only (§2, §11: no intraday).

series_id ``<SYMBOL>:spot_close:<exchange>``, e.g. ``BTC:spot_close:coinbase``.
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


def _ms_to_iso_day(ms: int) -> str:
    """Millisecond epoch -> ISO8601 UTC date at midnight (daily bucket)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")


class SpotPricesIngester(Ingester):
    """Fetch daily spot closes for BTC/ETH on Coinbase and Binance (for the premium)."""

    source = "spot_prices"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        cfg = settings.source("spot_prices")
        self._exchange_ids: list[str] = cfg.get("exchanges", ["coinbase", "binance"])
        self._assets: list[str] = cfg.get("assets", ["BTC", "ETH"])
        self._quotes: dict[str, str] = cfg.get(
            "quotes", {"coinbase": "USD", "binance": "USDT"}
        )
        self._history_days = int(cfg.get("history_days", 30))
        self._history_since = cfg.get("history_since", "2017-01-01T00:00:00Z")
        self._timeout = int(cfg.get("request_timeout_ms", 20000))

    def _make_exchange(self, exchange_id: str) -> ccxt.Exchange:
        exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": self._timeout})
        exchange.load_markets()
        return exchange

    def _close_rows(
        self, exchange: ccxt.Exchange, exchange_id: str, symbol: str, market: str
    ) -> list[dict[str, Any]]:
        """Daily spot close rows for one (exchange, asset)."""
        if not exchange.has.get("fetchOHLCV"):
            return []
        candles = retry(
            lambda: exchange.fetch_ohlcv(market, timeframe="1d", limit=self._history_days),
            exceptions=(ccxt.NetworkError,),
        )
        rows: list[dict[str, Any]] = []
        for ts, _open, _high, _low, close, _vol in candles:
            if close is None or ts is None:
                continue
            rows.append(
                {
                    "source": self.source,
                    "series_id": f"{symbol}:spot_close:{exchange_id}",
                    "ts": _ms_to_iso_day(ts),
                    "ts_release": None,
                    "value": float(close),
                }
            )
        return rows

    def fetch(self) -> pd.DataFrame:
        """Return daily spot closes across exchanges as a long observations DataFrame."""
        rows: list[dict[str, Any]] = []
        for exchange_id in self._exchange_ids:
            quote = self._quotes.get(exchange_id, "USD")
            try:
                exchange = retry(
                    lambda eid=exchange_id: self._make_exchange(eid),
                    exceptions=(ccxt.NetworkError,),
                )
            except Exception:  # noqa: BLE001 — isolate a dead exchange, keep the rest
                log.exception("spot_prices: could not initialize exchange %s", exchange_id)
                continue
            for symbol in self._assets:
                market = f"{symbol}/{quote}"
                if market not in exchange.markets:
                    log.debug("spot_prices: %s has no %s market; skipping", exchange_id, market)
                    continue
                try:
                    rows.extend(self._close_rows(exchange, exchange_id, symbol, market))
                except Exception:  # noqa: BLE001 — per (exchange, asset) isolation
                    log.exception("spot_prices: %s %s failed", exchange_id, market)
            log.info("spot_prices: %s done (%d rows so far).", exchange_id, len(rows))
        df = pd.DataFrame(rows, columns=OBSERVATION_COLUMNS)
        return self.validate(df)

    def fetch_history(self, days: int | None = None) -> pd.DataFrame:
        """Parte G (G5-a): full multi-year daily close history from **Binance** for all tracked assets.

        Binance gives years of free daily OHLCV (vs. CoinGecko's 365-day cap) with no key — the
        history the DCA allocator's cheapness z-score (200 d) and backtest need. Stores
        ``<SYMBOL>:spot_close:binance``. Paginated (Binance caps ~1000 candles/call); idempotent
        upsert dedupes overlaps. §9: this is a **separate** series from ``<cid>:price`` (CoinGecko)
        — the allocator/backtest use Binance (more history, and where you actually execute); the
        panel keeps CoinGecko. Invoked via ``run_ingest.py --backfill``.
        """
        try:
            exchange = retry(lambda: self._make_exchange("binance"), exceptions=(ccxt.NetworkError,))
        except Exception:  # noqa: BLE001 — no Binance, no history; skip cleanly
            log.exception("spot_prices history: could not initialize Binance")
            return self.validate(pd.DataFrame(columns=OBSERVATION_COLUMNS))

        quote = self._quotes.get("binance", "USDT")
        symbols = [a["symbol"] for a in self._settings.assets]
        since_floor = exchange.parse8601(self._history_since)
        now_ms = exchange.milliseconds()
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            market = f"{symbol}/{quote}"
            if market not in exchange.markets:
                continue
            since = since_floor
            count = 0
            while since < now_ms:
                candles = retry(
                    lambda m=market, s=since: exchange.fetch_ohlcv(m, "1d", since=s, limit=1000),
                    exceptions=(ccxt.NetworkError,),
                )
                if not candles:
                    break
                for ts, _o, _h, _low, close, _v in candles:
                    if close is not None and ts is not None:
                        rows.append(
                            {
                                "source": self.source,
                                "series_id": f"{symbol}:spot_close:binance",
                                "ts": _ms_to_iso_day(ts),
                                "ts_release": None,
                                "value": float(close),
                            }
                        )
                count += len(candles)
                last = candles[-1][0]
                if last < since + 86_400_000:  # no forward progress -> stop (guards against a loop)
                    break
                since = last + 86_400_000
            log.info("spot_prices history: %s -> %d daily candles.", symbol, count)
        return self.validate(pd.DataFrame(rows, columns=OBSERVATION_COLUMNS))

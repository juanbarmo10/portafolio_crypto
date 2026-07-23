"""Binance account sync — READ-ONLY (CLAUDE.md sections 2, 8 level 4, 11).

Pulls the user's real **holdings** (balances) and **trade history** from their Binance
account so the execution view (level 4) reflects actual positions, cost basis and fees
rather than a manual plan. Fees are material for ~$17 tickets (§1/§4).

SECURITY — this module only ever reads. It calls ``fetch_balance`` and
``fetch_my_trades``; it never places, cancels or withdraws anything. The API key must
be created READ-ONLY (no trading, no withdrawal) and IP-restricted — see
``config/.env.example``. Trading via API would violate the anti-over-trading and
no-leverage design (§2, §11); it is intentionally absent.

Requires BINANCE_API_KEY / BINANCE_API_SECRET. If absent the runner skips this ingester.

Produces two outputs:
    fetch()        -> holdings as observations[source, series_id, ts, ts_release, value]
                      series_id = "<ASSET>:balance" (total amount held), source "binance".
    fetch_trades() -> executed trades for the `trades` table (see db.loader.upsert_trades).
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

TRADE_COLUMNS = [
    "trade_id", "exchange", "symbol", "side", "ts",
    "price", "amount", "cost", "fee", "fee_currency",
]

# Wallet name -> ccxt fetch_balance 'type' param. "earn" is special (Simple Earn
# positions via sapi); the rest go through fetch_balance.
_WALLET_TYPE = {
    "spot": "spot",
    "funding": "funding",
    "future": "future",      # USD-M; needs "Enable Futures" permission (a trading perm)
    "delivery": "delivery",  # COIN-M; same caveat
    "margin": "margin",
}


def parse_earn(flexible: object, locked: object) -> dict[str, float]:
    """Sum Simple Earn (flexible + locked) positions per asset. Pure, testable.

    Binance returns ``{"rows": [{"asset": ..., "totalAmount"/"amount": ...}], ...}``.
    Flexible rows expose ``totalAmount``; locked rows expose ``amount``. This is the
    authoritative source for Earn (the LD-prefixed amounts inside the spot balance can
    be stale). Rows missing an asset or amount are skipped.
    """
    out: dict[str, float] = {}
    for payload in (flexible, locked):
        rows = payload.get("rows", []) if isinstance(payload, dict) else (payload or [])
        for row in rows:
            asset = row.get("asset")
            amount = _as_float(row.get("totalAmount", row.get("amount")))
            if not asset or amount is None:
                continue
            out[asset] = out.get(asset, 0.0) + amount
    return out


def parse_trades(raw_trades: list[dict], exchange: str) -> list[dict[str, Any]]:
    """Convert ccxt trade dicts to trades-table rows. Pure (no network), testable.

    Args:
        raw_trades: List of ccxt unified trade dicts (from fetch_my_trades).
        exchange: Exchange id to stamp (e.g. "binance").

    Returns:
        List of row dicts matching TRADE_COLUMNS. Trades without an id or timestamp
        are skipped (cannot dedupe them safely).
    """
    rows: list[dict[str, Any]] = []
    for trade in raw_trades:
        trade_id = trade.get("id")
        ts = trade.get("timestamp")
        if trade_id is None or ts is None:
            continue
        fee = trade.get("fee") or {}
        rows.append(
            {
                "trade_id": f"{exchange}:{trade_id}",
                "exchange": exchange,
                "symbol": trade.get("symbol"),
                "side": trade.get("side"),
                "ts": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
                "price": _as_float(trade.get("price")),
                "amount": _as_float(trade.get("amount")),
                "cost": _as_float(trade.get("cost")),
                "fee": _as_float(fee.get("cost")),
                "fee_currency": fee.get("currency"),
            }
        )
    return rows


def _as_float(value: object) -> float | None:
    """Coerce to float, or None if missing/non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BinanceAccountIngester(Ingester):
    """Read-only Binance holdings (via fetch()) and trade history (via fetch_trades())."""

    source = "binance"

    def __init__(self, settings: Settings) -> None:
        """Build a read-only ccxt client from BINANCE_API_KEY/SECRET.

        Raises:
            RuntimeError: If the API keys are not set (runner skips this ingester).
        """
        self._settings = settings
        cfg = settings.source("binance_account")
        self._quote: str = cfg.get("quote", "USDT")
        self._trades_since_days: int = int(cfg.get("trades_since_days", 180))
        self._timeout: int = int(cfg.get("request_timeout_ms", 20000))
        self._wallets: list[str] = cfg.get("wallets", ["spot", "funding", "earn"])
        self._tracked: list[str] = [a["symbol"] for a in settings.assets]

        api_key = settings.secrets.get("BINANCE_API_KEY")
        api_secret = settings.secrets.get("BINANCE_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError("BINANCE_API_KEY/SECRET not set; cannot construct account ingester.")

        # Read-only client: we only ever call fetch_balance / fetch_my_trades below.
        self._exchange = ccxt.binance(
            {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True, "timeout": self._timeout}
        )
        self._balances: dict[str, float] = {}

    def _earn_balances(self) -> dict[str, float]:
        """Simple Earn (flexible + locked) positions per asset — authoritative source."""
        flexible = retry(
            lambda: self._exchange.sapiGetSimpleEarnFlexiblePosition({"size": 100}),
            exceptions=(ccxt.NetworkError,),
        )
        locked = retry(
            lambda: self._exchange.sapiGetSimpleEarnLockedPosition({"size": 100}),
            exceptions=(ccxt.NetworkError,),
        )
        return parse_earn(flexible, locked)

    def _wallet_balances(self, wallet: str) -> dict[str, float]:
        """Return {asset: amount} for one wallet. 'earn' uses the Simple Earn endpoint."""
        if wallet == "earn":
            return self._earn_balances()
        params = {} if wallet == "spot" else {"type": _WALLET_TYPE[wallet]}
        balance = retry(
            lambda: self._exchange.fetch_balance(params), exceptions=(ccxt.NetworkError,)
        )
        return {a: float(v) for a, v in (balance.get("total") or {}).items() if v}

    def fetch(self) -> pd.DataFrame:
        """Return holdings per (asset, wallet) as observations, across configured wallets.

        series_id = "<ASSET>:balance:<wallet>". Earn is read from the authoritative Simple
        Earn endpoint; the matching LD-prefixed duplicates (LDBTC for BTC in Earn, ...) are
        then excluded from the spot balance to avoid double-counting. A wallet that is
        unknown or not accessible is logged and skipped — never aborts the run. Per-asset
        totals are cached for trade-symbol discovery.
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
        self._balances = {}
        rows: list[dict[str, Any]] = []

        # Fetch Earn first (if configured) so we can drop the LD-duplicates from spot.
        earn_balances: dict[str, float] = {}
        if "earn" in self._wallets:
            try:
                earn_balances = self._earn_balances()
            except Exception:  # noqa: BLE001 — fall back to spot LD* if Earn is unreachable
                log.exception("Binance: Simple Earn not accessible; spot LD* used as fallback.")
        ld_duplicates = {f"LD{asset}" for asset in earn_balances}

        for wallet in self._wallets:
            if wallet != "earn" and wallet not in _WALLET_TYPE:
                log.warning("Binance: unknown wallet '%s' in config; skipping.", wallet)
                continue
            try:
                balances = earn_balances if wallet == "earn" else self._wallet_balances(wallet)
            except Exception:  # noqa: BLE001 — a wallet may lack permission; isolate it
                log.exception("Binance: wallet '%s' not accessible; skipping.", wallet)
                continue
            for asset, amount in balances.items():
                if not amount:
                    continue
                if wallet == "spot" and asset in ld_duplicates:
                    continue  # captured accurately via the Earn endpoint
                self._balances[asset] = self._balances.get(asset, 0.0) + amount
                rows.append(
                    {
                        "source": self.source,
                        "series_id": f"{asset}:balance:{wallet}",
                        "ts": ts,
                        "ts_release": None,
                        "value": amount,
                    }
                )
            log.info("Binance wallet '%s': %d assets with balance.", wallet, len(balances))

        df = pd.DataFrame(rows, columns=OBSERVATION_COLUMNS)
        return self.validate(df)

    def fetch_trades(self) -> pd.DataFrame:
        """Return executed trades (READ-ONLY) for tracked + currently-held pairs.

        Scans ``<ASSET>/<quote>`` for the union of tracked assets (§5) and assets with
        a non-zero balance. Markets that don't exist or have no trades are skipped.
        """
        self._exchange.load_markets()
        since = self._exchange.milliseconds() - self._trades_since_days * 24 * 3600 * 1000

        held = {a for a in self._balances if a != self._quote}
        symbols = sorted({f"{a}/{self._quote}" for a in set(self._tracked) | held})

        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            if symbol not in self._exchange.markets:
                continue
            try:
                raw = retry(
                    lambda s=symbol: self._exchange.fetch_my_trades(s, since=since, limit=1000),
                    exceptions=(ccxt.NetworkError,),
                )
            except Exception:  # noqa: BLE001 — isolate per-symbol failures
                log.exception("Binance trades: %s failed", symbol)
                continue
            rows.extend(parse_trades(raw, self.source))

        df = pd.DataFrame(rows, columns=TRADE_COLUMNS)
        log.info("Binance trades: %d fills across %d symbols.", len(df), len(symbols))
        return df

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

CAPITAL_FLOW_COLUMNS = ["flow_id", "kind", "asset", "amount", "usd_value", "ts"]

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


def parse_earn_rewards(*responses: object) -> dict[str, float]:
    """Sum Simple Earn **reward** amounts per asset (passive yield). Pure, testable.

    Each response is ``{"rows": [{"asset": ..., "rewards": ...}], ...}``. Rewards are paid
    in the earning asset; summing over a window gives yield earned in that period.
    """
    totals: dict[str, float] = {}
    for resp in responses:
        rows = resp.get("rows", []) if isinstance(resp, dict) else []
        for row in rows:
            asset = row.get("asset")
            amount = _as_float(row.get("rewards"))
            if asset and amount:
                totals[asset] = totals.get(asset, 0.0) + amount
    return totals


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


_STABLES = {"USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD"}


def parse_convert(records: list[dict], stables: set[str] | None = None) -> list[dict[str, Any]]:
    """Convert Binance **Convert** records to trades-table rows (pure, testable).

    A convert ``fromAsset -> toAsset`` is a **buy** of toAsset when paid with a stablecoin,
    or a **sell** of fromAsset when received into a stablecoin — this recovers cost basis
    for tokens acquired via Convert (which never appear in spot fills). Crypto->crypto (no
    stable leg) is skipped: its USD cost basis can't be derived here. Keyed by orderId so
    re-ingesting is idempotent. Only SUCCESS orders are kept.
    """
    stables = stables or _STABLES
    rows: list[dict[str, Any]] = []
    for rec in records:
        if str(rec.get("orderStatus", "SUCCESS")).upper() != "SUCCESS":
            continue
        oid = rec.get("orderId")
        from_a, to_a = rec.get("fromAsset"), rec.get("toAsset")
        from_amt, to_amt = _as_float(rec.get("fromAmount")), _as_float(rec.get("toAmount"))
        ts_ms = rec.get("createTime")
        if oid is None or not (from_a and to_a and from_amt and to_amt and ts_ms):
            continue
        if from_a in stables and to_a not in stables:
            symbol, side, amount, cost = f"{to_a}/{from_a}", "buy", to_amt, from_amt
        elif to_a in stables and from_a not in stables:
            symbol, side, amount, cost = f"{from_a}/{to_a}", "sell", from_amt, to_amt
        else:
            continue  # crypto->crypto or stable->stable: no USD cost basis to derive
        rows.append(
            {
                "trade_id": f"binance-convert:{oid}",
                "exchange": "binance-convert",
                "symbol": symbol,
                "side": side,
                "ts": datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
                "price": (cost / amount) if amount else None,
                "amount": amount,
                "cost": cost,
                "fee": 0.0,
                "fee_currency": None,
            }
        )
    return rows


def parse_fiat_flows(
    records: list[dict], kind: str, fx_to_usd: dict[str, float]
) -> list[dict[str, Any]]:
    """Binance fiat orders -> capital_flows rows (pure, testable).

    ``kind`` is 'deposit' or 'withdraw'. USD value uses ``fx_to_usd`` (USD assumed 1.0);
    an unknown currency yields usd_value None (unconverted, counted but not summed). Only
    completed orders are kept. Keyed by orderNo for idempotency.
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        if str(rec.get("status", "")).lower() not in ("completed", "successful", "success"):
            continue
        order_no = rec.get("orderNo")
        currency = rec.get("fiatCurrency")
        amount = _as_float(rec.get("amount"))
        ts_ms = rec.get("createTime")
        if order_no is None or not currency or amount is None or not ts_ms:
            continue
        rate = fx_to_usd.get(currency.upper())
        rows.append(
            {
                "flow_id": f"binance:{kind}:{order_no}",
                "kind": kind,
                "asset": currency.upper(),
                "amount": amount,
                "usd_value": (amount * rate) if rate is not None else None,
                "ts": datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
            }
        )
    return rows


def parse_fiat_payments(
    records: list[dict], kind: str, fx_to_usd: dict[str, float]
) -> list[dict[str, Any]]:
    """Binance fiat **payments** (buy/sell crypto with fiat, e.g. PSE/card) -> capital_flows.

    A buy spends fiat (``sourceAmount`` in ``fiatCurrency``) -> deposit of capital; a sell
    receives fiat (``obtainAmount``) -> withdraw. Only completed orders. Keyed by orderNo.
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        if str(rec.get("status", "")).lower() not in ("completed", "successful", "success"):
            continue
        order_no = rec.get("orderNo")
        currency = rec.get("fiatCurrency")
        fiat_amt = _as_float(rec.get("sourceAmount") if kind == "deposit" else rec.get("obtainAmount"))
        ts_ms = rec.get("createTime")
        if order_no is None or not currency or fiat_amt is None or not ts_ms:
            continue
        rate = fx_to_usd.get(currency.upper())
        rows.append(
            {
                "flow_id": f"binance:payment:{order_no}",
                "kind": kind,
                "asset": currency.upper(),
                "amount": fiat_amt,
                "usd_value": (fiat_amt * rate) if rate is not None else None,
                "ts": datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
            }
        )
    return rows


def parse_c2c_orders(records: list[dict], fx_to_usd: dict[str, float]) -> list[dict[str, Any]]:
    """Binance **P2P (C2C)** orders -> capital_flows. Pure, testable.

    A P2P BUY pays fiat (``totalPrice`` in ``fiat``) to an external seller -> deposit of
    capital; a SELL receives fiat -> withdraw. Only completed orders. Keyed by orderNumber.
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        if str(rec.get("orderStatus", "")).upper() != "COMPLETED":
            continue
        order_no = rec.get("orderNumber")
        currency = rec.get("fiat")
        fiat_amt = _as_float(rec.get("totalPrice"))
        trade_type = str(rec.get("tradeType", "")).upper()
        ts_ms = rec.get("createTime")
        if order_no is None or not currency or fiat_amt is None or trade_type not in ("BUY", "SELL"):
            continue
        rate = fx_to_usd.get(currency.upper())
        rows.append(
            {
                "flow_id": f"binance:p2p:{order_no}",
                "kind": "deposit" if trade_type == "BUY" else "withdraw",
                "asset": currency.upper(),
                "amount": fiat_amt,
                "usd_value": (fiat_amt * rate) if rate is not None else None,
                "ts": datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
            }
        )
    return rows


class BinanceAccountIngester(Ingester):
    """Read-only Binance holdings (via fetch()) and trade history (via fetch_trades())."""

    source = "binance"

    def __init__(self, settings: Settings, full_history: bool = False) -> None:
        """Build a read-only ccxt client from BINANCE_API_KEY/SECRET.

        Args:
            full_history: deep reconstruction (Convert/Earn/capital flows over
                ``history_since_days``) — slow, use only for ``run_ingest.py --backfill``.
                Daily runs use the short ``account_recent_days`` window so they stay fast.

        Raises:
            RuntimeError: If the API keys are not set (runner skips this ingester).
        """
        self._settings = settings
        cfg = settings.source("binance_account")
        self._quote: str = cfg.get("quote", "USDT")
        self._trades_since_days: int = int(cfg.get("trades_since_days", 180))
        # Deep history (Convert/Earn/capital flows) is chunked into many API calls; running
        # it every day is slow, so daily uses a short window and --backfill goes deep.
        history_days = int(cfg.get("history_since_days", 365))
        self._account_days: int = history_days if full_history else int(
            cfg.get("account_recent_days", 60)
        )
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

    def _earn_rewards(self) -> dict[str, float]:
        """Simple Earn reward amounts per asset (flexible + locked), in <=30-day windows.

        The rewards-history endpoint rejects wide ranges (error -6021), so the lookback is
        chunked; per-window failures are isolated and the rest still sum.
        """
        now = self._exchange.milliseconds()
        since = now - self._account_days * 24 * 3600 * 1000
        window = 30 * 24 * 3600 * 1000
        responses: list[object] = []
        start = since
        while start < now:
            end = min(start + window, now)
            try:
                responses.append(
                    retry(
                        lambda s=start, e=end: self._exchange.sapiGetSimpleEarnFlexibleHistoryRewardsRecord(
                            {"type": "REWARDS", "startTime": s, "endTime": e, "size": 100}
                        ),
                        exceptions=(ccxt.NetworkError,),
                    )
                )
                responses.append(
                    retry(
                        lambda s=start, e=end: self._exchange.sapiGetSimpleEarnLockedHistoryRewardsRecord(
                            {"startTime": s, "endTime": e, "size": 100}
                        ),
                        exceptions=(ccxt.NetworkError,),
                    )
                )
            except Exception:  # noqa: BLE001 — isolate a bad window, keep the rest
                log.exception("Binance earn rewards: window %s-%s failed", start, end)
            start = end
        return parse_earn_rewards(*responses)

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

        # Passive yield: sum Simple Earn rewards over the window -> earn:<asset>:rewards.
        try:
            for asset, amount in self._earn_rewards().items():
                rows.append(
                    {
                        "source": self.source,
                        "series_id": f"earn:{asset}:rewards",
                        "ts": ts,
                        "ts_release": None,
                        "value": amount,
                    }
                )
        except Exception:  # noqa: BLE001 — Earn rewards optional; never abort holdings
            log.exception("Binance: Earn rewards not accessible; skipping.")

        df = pd.DataFrame(rows, columns=OBSERVATION_COLUMNS)
        return self.validate(df)

    def _fetch_convert_raw(self, since_ms: int) -> list[dict]:
        """Raw Binance Convert records since ``since_ms``, in <=30-day windows (API cap)."""
        now = self._exchange.milliseconds()
        window = 30 * 24 * 3600 * 1000
        out: list[dict] = []
        start = since_ms
        while start < now:
            end = min(start + window, now)
            try:
                resp = retry(
                    lambda s=start, e=end: self._exchange.sapiGetConvertTradeFlow(
                        {"startTime": s, "endTime": e, "limit": 1000}
                    ),
                    exceptions=(ccxt.NetworkError,),
                )
                out.extend(resp.get("list", []) or [])
            except Exception:  # noqa: BLE001 — isolate per-window failures
                log.exception("Binance convert: window %s-%s failed", start, end)
            start = end
        return out

    def fetch_trades(self) -> pd.DataFrame:
        """Return executed trades (READ-ONLY): spot fills **and** Convert conversions.

        Spot: scans ``<ASSET>/<quote>`` for tracked (§5) + held assets. Convert: recovers
        cost basis for tokens bought via Binance Convert (never appear as spot fills — the
        reason some holdings had no cost basis). Markets/windows that fail are skipped.
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

        spot_n = len(rows)
        try:
            convert_since = self._exchange.milliseconds() - self._account_days * 24 * 3600 * 1000
            rows.extend(parse_convert(self._fetch_convert_raw(convert_since)))
        except Exception:  # noqa: BLE001 — convert must not sink the spot trades
            log.exception("Binance convert fetch failed")

        df = pd.DataFrame(rows, columns=TRADE_COLUMNS)
        log.info(
            "Binance trades: %d spot fills (%d symbols) + %d convert -> %d total.",
            spot_n, len(symbols), len(rows) - spot_n, len(df),
        )
        return df

    def fetch_capital_flows(self) -> pd.DataFrame:
        """Fiat deposits/withdrawals (READ-ONLY) -> capital_flows rows (net capital in/out).

        USD conversion via ``binance_account.fx_to_usd`` config (USD assumed 1.0); non-USD
        fiat without a rate is stored unconverted. Windowed by 90 days (fiat-orders cap).
        """
        cfg = self._settings.source("binance_account")
        fx = {k.upper(): float(v) for k, v in (cfg.get("fx_to_usd", {}) or {}).items()}
        fx.setdefault("USD", 1.0)
        now = self._exchange.milliseconds()
        since = now - self._account_days * 24 * 3600 * 1000
        day = 24 * 3600 * 1000
        rows: list[dict[str, Any]] = []

        def _windows(width_days: int):
            start = since
            while start < now:
                end = min(start + width_days * day, now)
                yield start, end
                start = end

        # Fiat orders: direct fiat balance deposits/withdrawals (90-day windows).
        for kind, tx_type in (("deposit", "0"), ("withdraw", "1")):
            for s, e in _windows(90):
                try:
                    resp = retry(
                        lambda s=s, e=e, t=tx_type: self._exchange.sapiGetFiatOrders(
                            {"transactionType": t, "beginTime": s, "endTime": e, "rows": 500}
                        ),
                        exceptions=(ccxt.NetworkError,),
                    )
                    rows.extend(parse_fiat_flows(resp.get("data", []) or [], kind, fx))
                except Exception:  # noqa: BLE001 — isolate per-window failures
                    log.exception("Binance fiat orders %s: window failed", kind)

        # Fiat payments: buy/sell crypto with fiat (e.g. PSE, card) — capital in/out.
        for kind, tx_type in (("deposit", "0"), ("withdraw", "1")):
            for s, e in _windows(90):
                try:
                    resp = retry(
                        lambda s=s, e=e, t=tx_type: self._exchange.sapiGetFiatPayments(
                            {"transactionType": t, "beginTime": s, "endTime": e, "rows": 500}
                        ),
                        exceptions=(ccxt.NetworkError,),
                    )
                    rows.extend(parse_fiat_payments(resp.get("data", []) or [], kind, fx))
                except Exception:  # noqa: BLE001
                    log.exception("Binance fiat payments %s: window failed", kind)

        # P2P (C2C): buy/sell crypto peer-to-peer with fiat — capital in/out (30-day windows).
        for trade_type in ("BUY", "SELL"):
            for s, e in _windows(30):
                try:
                    resp = retry(
                        lambda s=s, e=e, t=trade_type: self._exchange.sapiGetC2cOrderMatchListUserOrderHistory(
                            {"tradeType": t, "startTimestamp": s, "endTimestamp": e, "rows": 100}
                        ),
                        exceptions=(ccxt.NetworkError,),
                    )
                    rows.extend(parse_c2c_orders(resp.get("data", []) or [], fx))
                except Exception:  # noqa: BLE001
                    log.exception("Binance P2P %s: window failed", trade_type)

        df = pd.DataFrame(rows, columns=CAPITAL_FLOW_COLUMNS)
        log.info("Binance capital flows: %d rows (fiat orders + payments + P2P).", len(df))
        return df

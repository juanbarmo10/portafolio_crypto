"""Liquidations collector (CLAUDE.md sections 4, 8; level 2).

Binance publishes forced liquidations only on the ``!forceOrder@arr`` WebSocket stream
(no free historical REST), so ``run_liquidations.py`` runs this as a small daemon: it
parses each event, buckets USD liquidation volume per (asset, UTC day, side), and flushes
running daily totals to ``observations`` (idempotent). This is **public** data (Q2 —
liquidation cascades / short-squeeze fuel), synced to the cloud DB like other market data.

Series: ``<SYMBOL>:liq_long:binance`` / ``<SYMBOL>:liq_short:binance`` (daily USD notional).
A forced SELL liquidates a **long**; a forced BUY liquidates a **short**.

The parser and buffer here are pure and unit-tested; the daemon (network) lives in
run_liquidations.py.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_force_order(event: dict, quote: str = "USDT") -> dict | None:
    """One Binance forceOrder event -> {base, side, usd, day} (pure), or None if invalid.

    ``side`` is 'long' (forced SELL) or 'short' (forced BUY). USD notional = filled qty ×
    average price. ``day`` is the UTC date of the order (falls back to event time E).
    """
    order = event.get("o") if isinstance(event, dict) and "o" in event else event
    if not isinstance(order, dict):
        return None
    symbol = order.get("s")
    side_raw = order.get("S")
    qty = _to_float(order.get("z") or order.get("q"))
    price = _to_float(order.get("ap") or order.get("p"))
    if not symbol or side_raw not in ("SELL", "BUY") or not qty or not price:
        return None
    if not symbol.endswith(quote):
        return None
    ms = order.get("T") or event.get("E")
    day = (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if ms
        else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    return {
        "base": symbol[: -len(quote)],
        "side": "long" if side_raw == "SELL" else "short",
        "usd": qty * price,
        "day": day,
    }


class LiquidationBuffer:
    """Accumulates USD liquidation volume per (asset, day, side); emits observation rows.

    Only tracked assets are kept. ``rows()`` returns the current running totals as long-
    format observations (idempotent upsert overwrites the day's total as it grows).
    """

    def __init__(self, tracked: set[str]):
        self._tracked = tracked
        self._totals: dict[tuple[str, str, str], float] = defaultdict(float)

    def add_event(self, event: dict, quote: str = "USDT") -> bool:
        parsed = parse_force_order(event, quote)
        if parsed is None or parsed["base"] not in self._tracked:
            return False
        self._totals[(parsed["base"], parsed["day"], parsed["side"])] += parsed["usd"]
        return True

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "source": "derivatives",
                "series_id": f"{base}:liq_{side}:binance",
                "ts": f"{day}T00:00:00+00:00",
                "ts_release": None,
                "value": total,
            }
            for (base, day, side), total in self._totals.items()
        ]

    def prune_before(self, day: str) -> None:
        """Drop buckets for days strictly before ``day`` (already flushed and final)."""
        for key in [k for k in self._totals if k[1] < day]:
            del self._totals[key]

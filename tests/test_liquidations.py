"""Tests for the liquidations parser + buffer (pure; the daemon's I/O is not tested)."""

from __future__ import annotations

from db.loader import OBSERVATION_COLUMNS
from ingest.liquidations import LiquidationBuffer, parse_force_order


def _event(symbol, side, qty, price, ms=1_700_000_000_000):
    return {"e": "forceOrder", "E": ms, "o": {"s": symbol, "S": side, "q": qty, "ap": price, "T": ms}}


def test_parse_force_order_side_and_usd() -> None:
    # Forced SELL = a LONG was liquidated; USD = qty * avg price.
    p = parse_force_order(_event("BTCUSDT", "SELL", "0.5", "60000"))
    assert p == {"base": "BTC", "side": "long", "usd": 30000.0, "day": "2023-11-14"}
    # Forced BUY = a SHORT was liquidated.
    assert parse_force_order(_event("ETHUSDT", "BUY", "2", "3000"))["side"] == "short"
    # Non-USDT quote or malformed -> None.
    assert parse_force_order(_event("BTCBUSD", "SELL", "1", "60000")) is None
    assert parse_force_order({"o": {"s": "BTCUSDT", "S": "SELL"}}) is None


def test_liquidation_buffer_aggregates_tracked_only() -> None:
    buf = LiquidationBuffer(tracked={"BTC", "ETH"})
    assert buf.add_event(_event("BTCUSDT", "SELL", "0.5", "60000")) is True  # 30000 long
    assert buf.add_event(_event("BTCUSDT", "SELL", "0.5", "40000")) is True  # +20000 long
    assert buf.add_event(_event("BTCUSDT", "BUY", "1", "50000")) is True  # 50000 short
    assert buf.add_event(_event("DOGEUSDT", "SELL", "100", "1")) is False  # not tracked

    rows = {(r["series_id"]): r["value"] for r in buf.rows()}
    assert rows["BTC:liq_long:binance"] == 50000.0  # 30000 + 20000 (running daily total)
    assert rows["BTC:liq_short:binance"] == 50000.0
    for r in buf.rows():
        assert set(OBSERVATION_COLUMNS) <= set(r)
        assert r["source"] == "derivatives"


def test_liquidation_buffer_prune() -> None:
    buf = LiquidationBuffer(tracked={"BTC"})
    buf.add_event(_event("BTCUSDT", "SELL", "1", "1000", ms=1_700_000_000_000))  # 2023-11-14
    buf.prune_before("2023-11-15")
    assert buf.rows() == []

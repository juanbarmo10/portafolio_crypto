"""Tests for the read-only Binance account parsing (CLAUDE.md sections 8, 10).

parse_trades is pure (no network / no API key), so trade normalization is testable
without touching the exchange.
"""

from __future__ import annotations

from ingest.binance_account import TRADE_COLUMNS, parse_trades


def _ccxt_trade(**overrides) -> dict:
    base = {
        "id": "12345",
        "timestamp": 1_784_764_800_000,  # 2026-07-23T00:00:00Z
        "symbol": "BTC/USDT",
        "side": "buy",
        "price": 65000.0,
        "amount": 0.001,
        "cost": 65.0,
        "fee": {"cost": 0.065, "currency": "USDT"},
    }
    base.update(overrides)
    return base


def test_parse_trades_maps_fields() -> None:
    rows = parse_trades([_ccxt_trade()], "binance")
    assert len(rows) == 1
    row = rows[0]
    assert set(TRADE_COLUMNS) <= set(row)
    assert row["trade_id"] == "binance:12345"   # exchange-prefixed for global uniqueness
    assert row["side"] == "buy"
    assert row["price"] == 65000.0
    assert row["cost"] == 65.0
    assert row["fee"] == 0.065
    assert row["fee_currency"] == "USDT"
    assert row["ts"].startswith("2026-07-23")


def test_parse_trades_skips_without_id_or_timestamp() -> None:
    rows = parse_trades(
        [_ccxt_trade(id=None), _ccxt_trade(timestamp=None), _ccxt_trade(id="9")],
        "binance",
    )
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "binance:9"


def test_parse_trades_handles_missing_fee() -> None:
    rows = parse_trades([_ccxt_trade(fee=None)], "binance")
    assert rows[0]["fee"] is None
    assert rows[0]["fee_currency"] is None

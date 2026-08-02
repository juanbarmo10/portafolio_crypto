"""Tests for the read-only Binance account parsing (CLAUDE.md sections 8, 10).

parse_trades is pure (no network / no API key), so trade normalization is testable
without touching the exchange.
"""

from __future__ import annotations

import pytest

from ingest.binance_account import (
    CAPITAL_FLOW_COLUMNS,
    TRADE_COLUMNS,
    count_unlotted_converts,
    parse_c2c_orders,
    parse_convert,
    parse_earn,
    parse_earn_rewards,
    parse_fiat_flows,
    parse_fiat_payments,
    parse_trades,
)


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


def test_parse_convert_buy_sell_and_skips() -> None:
    records = [
        # USDT -> XRP: a BUY of XRP (recovers cost basis for a Convert-acquired token).
        {"orderId": 1, "orderStatus": "SUCCESS", "fromAsset": "USDT", "fromAmount": "100",
         "toAsset": "XRP", "toAmount": "50", "createTime": 1_700_000_000_000},
        # BNB -> USDT: a SELL of BNB.
        {"orderId": 2, "orderStatus": "SUCCESS", "fromAsset": "BNB", "fromAmount": "0.5",
         "toAsset": "USDT", "toAmount": "300", "createTime": 1_700_100_000_000},
        # BTC -> ETH: crypto->crypto, no stable leg -> skipped.
        {"orderId": 3, "orderStatus": "SUCCESS", "fromAsset": "BTC", "fromAmount": "0.01",
         "toAsset": "ETH", "toAmount": "0.3", "createTime": 1_700_200_000_000},
        # Not successful -> skipped.
        {"orderId": 4, "orderStatus": "PROCESS", "fromAsset": "USDT", "fromAmount": "10",
         "toAsset": "XRP", "toAmount": "5", "createTime": 1_700_300_000_000},
    ]
    rows = parse_convert(records)
    assert [r["trade_id"] for r in rows] == ["binance-convert:1", "binance-convert:2"]
    buy = rows[0]
    assert buy["symbol"] == "XRP/USDT" and buy["side"] == "buy"
    assert buy["amount"] == 50.0 and buy["cost"] == 100.0 and buy["price"] == 2.0
    assert set(TRADE_COLUMNS) <= set(buy)
    sell = rows[1]
    assert sell["symbol"] == "BNB/USDT" and sell["side"] == "sell"
    assert sell["amount"] == 0.5 and sell["cost"] == 300.0


def test_count_unlotted_converts_flags_crypto_to_crypto() -> None:
    # Only crypto->crypto counts (a taxable permuta, RESEARCH.md §17): stable legs are excluded.
    records = [
        {"orderStatus": "SUCCESS", "fromAsset": "SOL", "toAsset": "LINK"},   # crypto->crypto ✓
        {"orderStatus": "SUCCESS", "fromAsset": "USDT", "toAsset": "BTC"},   # buy (stable leg)
        {"orderStatus": "SUCCESS", "fromAsset": "BTC", "toAsset": "USDT"},   # sell (stable leg)
        {"orderStatus": "SUCCESS", "fromAsset": "USDT", "toAsset": "USDC"},  # stable->stable
        {"orderStatus": "PROCESS", "fromAsset": "SOL", "toAsset": "AAVE"},   # not successful
    ]
    assert count_unlotted_converts(records) == 1


def test_parse_earn_rewards_sums_per_asset() -> None:
    flexible = {"rows": [
        {"asset": "BTC", "rewards": "0.0001"},
        {"asset": "BTC", "rewards": "0.0002"},
        {"asset": "USDT", "rewards": "1.5"},
    ]}
    locked = {"rows": [{"asset": "USDT", "rewards": "0.5"}]}
    totals = parse_earn_rewards(flexible, locked)
    assert totals["BTC"] == pytest.approx(0.0003)
    assert totals["USDT"] == pytest.approx(2.0)


def test_parse_fiat_flows_usd_and_unconverted() -> None:
    records = [
        {"orderNo": "a", "fiatCurrency": "USD", "amount": "100", "status": "Completed",
         "createTime": 1_700_000_000_000},
        {"orderNo": "b", "fiatCurrency": "COP", "amount": "400000", "status": "Completed",
         "createTime": 1_700_100_000_000},  # converted via fx
        {"orderNo": "c", "fiatCurrency": "EUR", "amount": "50", "status": "Completed",
         "createTime": 1_700_200_000_000},  # no rate -> unconverted (usd None)
        {"orderNo": "d", "fiatCurrency": "USD", "amount": "9", "status": "Processing",
         "createTime": 1_700_300_000_000},  # not completed -> skipped
    ]
    rows = parse_fiat_flows(records, "deposit", {"USD": 1.0, "COP": 0.00025})
    assert [r["flow_id"] for r in rows] == ["binance:deposit:a", "binance:deposit:b",
                                            "binance:deposit:c"]
    assert set(CAPITAL_FLOW_COLUMNS) <= set(rows[0])
    assert rows[0]["usd_value"] == 100.0
    assert rows[1]["usd_value"] == pytest.approx(400000 * 0.00025)  # 100
    assert rows[2]["usd_value"] is None  # EUR unconverted
    assert all(r["kind"] == "deposit" for r in rows)


def test_parse_fiat_payments_pse_buy() -> None:
    # Buy crypto with COP (e.g. PSE): sourceAmount COP = capital in.
    records = [
        {"orderNo": "p1", "fiatCurrency": "COP", "sourceAmount": "400000", "obtainAmount": "100",
         "cryptoCurrency": "USDT", "status": "Completed", "createTime": 1_700_000_000_000},
    ]
    rows = parse_fiat_payments(records, "deposit", {"COP": 0.00025})
    assert rows[0]["flow_id"] == "binance:payment:p1"
    assert rows[0]["asset"] == "COP" and rows[0]["amount"] == 400000.0
    assert rows[0]["usd_value"] == pytest.approx(100.0)  # 400000 * 0.00025
    assert set(CAPITAL_FLOW_COLUMNS) <= set(rows[0])


def test_parse_c2c_orders_buy_and_sell() -> None:
    records = [
        {"orderNumber": "c1", "tradeType": "BUY", "fiat": "COP", "totalPrice": "200000",
         "asset": "USDT", "orderStatus": "COMPLETED", "createTime": 1_700_000_000_000},
        {"orderNumber": "c2", "tradeType": "SELL", "fiat": "COP", "totalPrice": "80000",
         "asset": "USDT", "orderStatus": "COMPLETED", "createTime": 1_700_100_000_000},
        {"orderNumber": "c3", "tradeType": "BUY", "fiat": "COP", "totalPrice": "1",
         "asset": "USDT", "orderStatus": "CANCELLED", "createTime": 1_700_200_000_000},  # skip
    ]
    rows = parse_c2c_orders(records, {"COP": 0.00025})
    assert [(r["flow_id"], r["kind"]) for r in rows] == [
        ("binance:p2p:c1", "deposit"), ("binance:p2p:c2", "withdraw")
    ]
    assert rows[0]["usd_value"] == pytest.approx(50.0)  # 200000 * 0.00025


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


# --- parse_earn (authoritative Simple Earn positions) ------------------------


def test_parse_earn_sums_flexible_and_locked() -> None:
    flexible = {"rows": [{"asset": "USDC", "totalAmount": "80.08"},
                         {"asset": "BTC", "totalAmount": "0.01121"}]}
    locked = {"rows": [{"asset": "BTC", "amount": "0.001"}]}
    result = parse_earn(flexible, locked)
    assert result["USDC"] == 80.08
    assert result["BTC"] == pytest.approx(0.01221)  # flexible + locked


def test_parse_earn_empty_and_malformed() -> None:
    assert parse_earn({"rows": []}, {"rows": []}) == {}
    assert parse_earn({"rows": [{"asset": "BTC"}, {"totalAmount": "1"}]}, {}) == {}

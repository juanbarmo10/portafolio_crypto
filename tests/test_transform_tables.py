"""Smoke tests for transform.indicators DB-backed table builders (CLAUDE.md section 8).

These seed a temporary DB with a few observations and assert the assembled tables
have the right shape and values — no network involved.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from core.config import load_settings
from db.loader import init_db, upsert_observations, upsert_trades
from transform.indicators import (
    dca_status,
    dca_vs_baseline_table,
    execution_summary,
    holdings_by_group,
    holdings_table,
    macro_table,
    portfolio_table,
    thesis_invalidation_table,
    thesis_tvl_table,
    value_accrual_table,
    wallet_pnl_table,
    wallet_value_history,
)


@pytest.fixture()
def settings():
    """The real settings object (asset universe drives the table builders).

    manual_holdings is reset to empty so tests are isolated from any real entry
    (e.g. trading-bot capital) configured in settings.yaml; tests that need it set
    it explicitly.
    """
    load_settings.cache_clear()
    s = load_settings()
    # Isolate tests from any real config/settings.local.yaml (manual_holdings / cost_basis).
    ba = s.raw.get("sources", {}).get("binance_account", {})
    ba["manual_holdings"] = []
    ba["cost_basis"] = {}
    yield s
    load_settings.cache_clear()


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = init_db(tmp_path / "t.db")
    yield connection
    connection.close()


def _obs(series_id: str, ts: str, value: float, source: str = "coingecko") -> dict:
    return {
        "source": source,
        "series_id": series_id,
        "ts": ts,
        "ts_release": None,
        "value": value,
    }


def test_portfolio_table_computes_dilution_and_dominance(conn, settings) -> None:
    df = pd.DataFrame(
        [
            _obs("bitcoin:price", "2026-07-22T00:00:00+00:00", 65000.0),
            _obs("bitcoin:market_cap", "2026-07-22T00:00:00+00:00", 1_300_000_000_000.0),
            _obs("bitcoin:ath", "2026-07-22T00:00:00+00:00", 100_000.0),
            _obs("bitcoin:circulating_supply", "2026-07-22T00:00:00+00:00", 19_700_000.0),
            _obs("bitcoin:max_supply", "2026-07-22T00:00:00+00:00", 21_000_000.0),
            _obs("ethereum:market_cap", "2026-07-22T00:00:00+00:00", 260_000_000_000.0),
        ]
    )
    upsert_observations(conn, df)

    table = portfolio_table(conn, settings)
    btc = table[table["symbol"] == "BTC"].iloc[0]
    assert btc["price"] == 65000.0
    # dist to ATH: 65000/100000 - 1 = -35%
    assert btc["dist_ath"] == pytest.approx(-35.0)
    # dilution 19.7M / 21M ~ 0.938 -> not a risk (> 0.6 default)
    assert btc["dilution_ratio"] == pytest.approx(19_700_000.0 / 21_000_000.0)
    assert not btc["dilution_risk"]
    # dominance present in attrs
    assert table.attrs["btc_dominance"] is not None


def test_portfolio_table_includes_asset_meta(conn, settings) -> None:
    # logo_url and description come from config/assets_meta.yaml, present for every row.
    table = portfolio_table(conn, settings)
    btc = table[table["symbol"] == "BTC"].iloc[0]
    assert btc["logo_url"]
    assert btc["description"]
    assert "volume_24h" in table.columns  # market-cap/volume columns for the Radar
    assert "next_unlock" in table.columns


def test_macro_table_carries_crypto_effect(conn, settings) -> None:
    upsert_observations(
        conn,
        pd.DataFrame([_obs("CPIAUCSL", "2026-06-01T00:00:00+00:00", 100.0, source="fred")]),
    )
    table = macro_table(conn, settings)
    cpi = table[table["series_id"] == "CPIAUCSL"].iloc[0]
    assert cpi["crypto_effect"] == "inverse"


def test_thesis_table_includes_all_assets(conn, settings) -> None:
    # Every tracked asset appears, even those without a tracked TVL (e.g. BTC).
    table = thesis_tvl_table(conn, settings)
    assert len(table) == len(settings.assets)
    btc = table[table["symbol"] == "BTC"].iloc[0]
    assert pd.isna(btc["tvl"])  # BTC has no defillama TVL
    assert btc["kind"] is None or pd.isna(btc["kind"])  # no TVL source
    assert btc["logo_url"]  # meta still attached


def test_thesis_table_grouped_by_category(conn, settings) -> None:
    # Same-category tokens are adjacent (sorted by category).
    cats = list(thesis_tvl_table(conn, settings)["thesis_category"])
    # Each category forms a single contiguous run.
    seen: set = set()
    prev = None
    for c in cats:
        if c != prev:
            assert c not in seen, f"category {c} is not contiguous"
            seen.add(c)
            prev = c


def test_thesis_tvl_table_7d_change(conn, settings) -> None:
    # Aave TVL: 100 eight days ago, 110 today -> +10% over 7d.
    df = pd.DataFrame(
        [
            _obs("aave:tvl", "2026-07-14T00:00:00+00:00", 100.0, source="defillama"),
            _obs("aave:tvl", "2026-07-22T00:00:00+00:00", 110.0, source="defillama"),
        ]
    )
    upsert_observations(conn, df)
    table = thesis_tvl_table(conn, settings)
    aave = table[table["symbol"] == "AAVE"].iloc[0]
    assert aave["tvl"] == 110.0
    assert aave["tvl_chg_7d"] == pytest.approx(10.0)


def test_macro_table_change_and_labels(conn, settings) -> None:
    # CPI: previous 100.0 then latest 101.0 -> +1.0% MoM change.
    df = pd.DataFrame(
        [
            _obs("CPIAUCSL", "2026-05-01T00:00:00+00:00", 100.0, source="fred"),
            _obs("CPIAUCSL", "2026-06-01T00:00:00+00:00", 101.0, source="fred"),
        ]
    )
    upsert_observations(conn, df)
    table = macro_table(conn, settings)
    cpi = table[table["series_id"] == "CPIAUCSL"].iloc[0]
    assert cpi["label"] == "CPI"  # display label from config, not the raw key
    assert cpi["description"]  # non-empty tooltip text
    assert cpi["value"] == 101.0
    assert cpi["change_pct"] == pytest.approx(1.0)


def test_macro_nfp_absolute_change(conn, settings) -> None:
    # NFP is configured as absolute display: report jobs added (in thousands).
    df = pd.DataFrame(
        [
            _obs("PAYEMS", "2026-05-01T00:00:00+00:00", 158834.0, source="fred"),
            _obs("PAYEMS", "2026-06-01T00:00:00+00:00", 158984.0, source="fred"),
        ]
    )
    upsert_observations(conn, df)
    table = macro_table(conn, settings)
    nfp = table[table["series_id"] == "PAYEMS"].iloc[0]
    assert nfp["change_display"] == "absolute"
    assert nfp["change_unit"] == "K"
    assert nfp["change_abs"] == pytest.approx(150.0)  # +150K jobs


def test_macro_change_none_without_previous(conn, settings) -> None:
    upsert_observations(
        conn,
        pd.DataFrame([_obs("DFF", "2026-07-21T00:00:00+00:00", 3.63, source="fred")]),
    )
    table = macro_table(conn, settings)
    dff = table[table["series_id"] == "DFF"].iloc[0]
    assert dff["value"] == 3.63
    assert dff["change_pct"] is None  # single observation -> no change


def test_holdings_table_values_and_weights(conn, settings) -> None:
    # 0.02 BTC @ 65000 = 1300; 500 USDT (cash) = 500; total 1800.
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-23T00:00:00+00:00", 65000.0),
                _obs("BTC:balance:spot", "2026-07-23T00:00:00+00:00", 0.02, source="binance"),
                _obs("USDT:balance:spot", "2026-07-23T00:00:00+00:00", 500.0, source="binance"),
            ]
        ),
    )
    table = holdings_table(conn, settings)
    assert table.attrs["total_value_usd"] == pytest.approx(1800.0)
    assert table.attrs["cash_usd"] == pytest.approx(500.0)
    btc = table[table["asset"] == "BTC"].iloc[0]
    assert btc["value_usd"] == pytest.approx(1300.0)
    assert btc["weight_pct"] == pytest.approx(1300.0 / 1800.0 * 100)


def test_holdings_table_decodes_earn_and_prices_wbeth(conn, settings) -> None:
    # BTC + flexible-Earn LDBTC merge to BTC; WBETH is priced via its own CoinGecko id
    # (wrapped-beacon-eth = $2071), NOT as ETH.
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-23T00:00:00+00:00", 65000.0),
                _obs("wrapped-beacon-eth:price", "2026-07-23T00:00:00+00:00", 2071.0),
                _obs("BTC:balance:spot", "2026-07-23T00:00:00+00:00", 0.01, source="binance"),
                _obs("LDBTC:balance:spot", "2026-07-23T00:00:00+00:00", 0.01, source="binance"),
                _obs("WBETH:balance:spot", "2026-07-23T00:00:00+00:00", 0.05, source="binance"),
            ]
        ),
    )
    table = holdings_table(conn, settings)
    btc = table[table["asset"] == "BTC"].iloc[0]
    assert btc["amount"] == pytest.approx(0.02)          # BTC + LDBTC merged
    assert btc["value_usd"] == pytest.approx(1300.0)
    wbeth = table[table["asset"] == "WBETH"].iloc[0]     # priced at its own value
    assert wbeth["value_usd"] == pytest.approx(0.05 * 2071.0)


def test_holdings_table_manual_entries(conn, settings) -> None:
    # Capital the read-only key can't read (grid bots) recorded manually.
    settings.raw["sources"]["binance_account"]["manual_holdings"] = [
        {"asset": "BTC", "amount": 0.002, "note": "grid bot"},
        {"asset": "USDT", "amount": 70, "note": "grid bot"},
    ]
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-23T00:00:00+00:00", 65000.0),
                _obs("USDT:balance:spot", "2026-07-23T00:00:00+00:00", 100.0, source="binance"),
            ]
        ),
    )
    table = holdings_table(conn, settings)
    manual = table[table["note"] == "grid bot"]
    assert set(manual["asset"]) == {"BTC", "USDT"}
    btc_bot = manual[manual["asset"] == "BTC"].iloc[0]
    assert btc_bot["value_usd"] == pytest.approx(130.0)   # 0.002 * 65000
    # Manual value is included in the total (100 USDT spot + 130 BTC bot + 70 USDT bot).
    assert table.attrs["total_value_usd"] == pytest.approx(300.0)


def test_holdings_table_manual_lump_sum(conn, settings) -> None:
    # A lump-sum USD entry (no per-asset amount), e.g. trading-bot capital.
    settings.raw["sources"]["binance_account"]["manual_holdings"] = [
        {"label": "Trading bots", "value_usd": 200, "note": "grid bots"}
    ]
    upsert_observations(
        conn,
        pd.DataFrame(
            [_obs("USDT:balance:spot", "2026-07-23T00:00:00+00:00", 100.0, source="binance")]
        ),
    )
    table = holdings_table(conn, settings)
    bots = table[table["asset"] == "Trading bots"].iloc[0]
    assert bots["value_usd"] == pytest.approx(200.0)
    assert bots["note"] == "grid bots"
    assert table.attrs["total_value_usd"] == pytest.approx(300.0)  # 100 spot + 200 bots


def test_holdings_table_ignores_legacy_unsuffixed_series(conn, settings) -> None:
    # Old <ASSET>:balance rows (pre multi-wallet) must not double-count.
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-23T00:00:00+00:00", 65000.0),
                _obs("BTC:balance", "2026-07-23T00:00:00+00:00", 0.5, source="binance"),   # legacy
                _obs("BTC:balance:spot", "2026-07-23T00:00:00+00:00", 0.02, source="binance"),
            ]
        ),
    )
    btc = holdings_table(conn, settings)
    row = btc[btc["asset"] == "BTC"].iloc[0]
    assert row["amount"] == pytest.approx(0.02)  # legacy 0.5 ignored


def test_holdings_table_empty_without_sync(conn, settings) -> None:
    assert holdings_table(conn, settings).empty


def test_holdings_table_drops_dust(conn, settings) -> None:
    # 0.00001 BTC @ 65000 = $0.65 < $1 dust threshold -> dropped.
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-23T00:00:00+00:00", 65000.0),
                _obs("BTC:balance:spot", "2026-07-23T00:00:00+00:00", 0.00001, source="binance"),
                _obs("USDT:balance:spot", "2026-07-23T00:00:00+00:00", 500.0, source="binance"),
            ]
        ),
    )
    table = holdings_table(conn, settings)
    assert "BTC" not in set(table["asset"])


def test_holdings_by_group_buckets_and_weights(conn, settings) -> None:
    # BTC (tracked) $1300 · USDT cash $500 · WBETH (untracked, not in §5) 0.05@2000 = $100.
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-23T00:00:00+00:00", 65000.0),
                _obs("wrapped-beacon-eth:price", "2026-07-23T00:00:00+00:00", 2000.0),
                _obs("BTC:balance:spot", "2026-07-23T00:00:00+00:00", 0.02, source="binance"),
                _obs("USDT:balance:spot", "2026-07-23T00:00:00+00:00", 500.0, source="binance"),
                _obs("WBETH:balance:spot", "2026-07-23T00:00:00+00:00", 0.05, source="binance"),
            ]
        ),
    )
    holdings = holdings_table(conn, settings)

    by_cat = holdings_by_group(holdings, settings, by="thesis_category")
    groups = dict(zip(by_cat["group"], by_cat["value_usd"], strict=False))
    assert groups["Efectivo"] == pytest.approx(500.0)  # stablecoin -> cash bucket
    assert groups["Otros"] == pytest.approx(100.0)  # WBETH not in §5 -> Otros
    btc_cat = next(a["thesis_category"] for a in settings.assets if a["symbol"] == "BTC")
    assert groups[btc_cat] == pytest.approx(1300.0)  # tracked asset under its category
    assert by_cat["weight_pct"].sum() == pytest.approx(100.0)

    by_asset = holdings_by_group(holdings, settings, by="asset")
    assert set(by_asset["group"]) == {"BTC", "WBETH", "Efectivo"}


def test_holdings_by_group_empty(settings) -> None:
    out = holdings_by_group(pd.DataFrame(), settings)
    assert out.empty
    assert list(out.columns) == ["group", "value_usd", "weight_pct"]


def test_thesis_invalidation_flags_tvl_drop(conn, settings) -> None:
    # A >20% TVL drop over 7d on a tracked protocol -> red status with a TVL reason.
    slug = next(a["defillama"]["slug"] for a in settings.assets if a["symbol"] == "AAVE")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=8)).strftime("%Y-%m-%dT00:00:00+00:00")
    recent = now.strftime("%Y-%m-%dT00:00:00+00:00")
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs(f"{slug}:tvl", old, 100.0, source="defillama"),
                _obs(f"{slug}:tvl", recent, 70.0, source="defillama"),
            ]
        ),
    )
    board = thesis_invalidation_table(conn, settings)
    aave = board[board["symbol"] == "AAVE"].iloc[0]
    assert aave["status"] == "red"
    assert "TVL" in aave["reason"]
    assert board.iloc[0]["status"] in {"red", "amber"}  # sorted worst-first


def test_value_accrual_table_needs_tvl_and_mcap(conn, settings) -> None:
    # AAVE has TVL 500M and mcap 2B -> MC/TVL = 4.0; included. No mcap -> excluded.
    slug = next(a["defillama"]["slug"] for a in settings.assets if a["symbol"] == "AAVE")
    cid = next(a["coingecko_id"] for a in settings.assets if a["symbol"] == "AAVE")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs(f"{slug}:tvl", now, 500_000_000.0, source="defillama"),
                _obs(f"{cid}:market_cap", now, 2_000_000_000.0, source="coingecko"),
            ]
        ),
    )
    df = value_accrual_table(conn, settings)
    aave = df[df["symbol"] == "AAVE"].iloc[0]
    assert aave["mc_tvl"] == pytest.approx(4.0)
    assert aave["tvl"] == pytest.approx(500_000_000.0)
    # An asset with TVL config but no market cap in the DB is excluded (no valuation).
    assert "XRP" not in set(df["symbol"])  # XRP has no defillama slug -> never included


def test_dca_vs_baseline_edge(conn, settings) -> None:
    cid = next(a["coingecko_id"] for a in settings.assets if a["symbol"] == "BTC")
    # Two buys: 0.01@100 and 0.01@200 -> avg entry 150 (invested 3 / tokens 0.02).
    upsert_trades(
        conn,
        pd.DataFrame(
            [
                {"trade_id": "1", "exchange": "binance", "symbol": "BTC/USDT", "side": "buy",
                 "ts": "2026-01-01T00:00:00+00:00", "price": 100.0, "amount": 0.01, "cost": 1.0,
                 "fee": 0.0, "fee_currency": "USDT"},
                {"trade_id": "2", "exchange": "binance", "symbol": "BTC/USDT", "side": "buy",
                 "ts": "2026-01-10T00:00:00+00:00", "price": 200.0, "amount": 0.01, "cost": 2.0,
                 "fee": 0.0, "fee_currency": "USDT"},
            ]
        ),
    )
    # Price history covering the window (min <= first buy). Window mean (>= 2026-01-01)
    # = mean(200, 300) = 250; current (latest) = 300.
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs(f"{cid}:price", "2025-12-31T00:00:00+00:00", 100.0),
                _obs(f"{cid}:price", "2026-01-05T00:00:00+00:00", 200.0),
                _obs(f"{cid}:price", "2026-01-15T00:00:00+00:00", 300.0),
            ]
        ),
    )
    btc = dca_vs_baseline_table(conn, settings).iloc[0]
    assert btc["avg_entry"] == pytest.approx(150.0)
    assert btc["current_price"] == pytest.approx(300.0)
    assert btc["actual_ret_pct"] == pytest.approx(100.0)  # 300/150 - 1
    assert btc["baseline_avg"] == pytest.approx(250.0)
    assert btc["baseline_ret_pct"] == pytest.approx(20.0)  # 300/250 - 1
    assert btc["edge_pp"] == pytest.approx(80.0)  # 100 - 20


def test_dca_vs_baseline_no_history_no_baseline(conn, settings) -> None:
    # Buys but no price history covering the window -> baseline is None (honest), actual still None.
    upsert_trades(
        conn,
        pd.DataFrame(
            [
                {"trade_id": "1", "exchange": "binance", "symbol": "BTC/USDT", "side": "buy",
                 "ts": "2026-01-01T00:00:00+00:00", "price": 100.0, "amount": 0.01, "cost": 1.0,
                 "fee": 0.0, "fee_currency": "USDT"},
            ]
        ),
    )
    btc = dca_vs_baseline_table(conn, settings).iloc[0]
    assert btc["baseline_avg"] is None
    assert btc["edge_pp"] is None


def test_wallet_pnl_from_trades(conn, settings) -> None:
    # 0.5 BTC bought at 100 (avg). Held 0.5, current 200 -> +100% / +$50.
    upsert_trades(
        conn,
        pd.DataFrame(
            [
                {"trade_id": "1", "exchange": "binance", "symbol": "BTC/USDT", "side": "buy",
                 "ts": "2026-01-01T00:00:00+00:00", "price": 100.0, "amount": 0.5, "cost": 50.0,
                 "fee": 0.0, "fee_currency": "USDT"},
            ]
        ),
    )
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-23T00:00:00+00:00", 200.0),
                _obs("BTC:balance:spot", "2026-07-23T00:00:00+00:00", 0.5, source="binance"),
            ]
        ),
    )
    btc = wallet_pnl_table(conn, settings).query("symbol == 'BTC'").iloc[0]
    assert btc["avg_price"] == pytest.approx(100.0)
    assert btc["cost_usd"] == pytest.approx(50.0)  # avg 100 * amount 0.5
    assert btc["value_usd"] == pytest.approx(100.0)
    assert btc["pnl_usd"] == pytest.approx(50.0)
    assert btc["pnl_pct"] == pytest.approx(100.0)
    assert btc["source"] == "trades"


def test_wallet_pnl_manual_overrides_trades(conn, settings) -> None:
    # Manual cost basis wins over the trade-derived average (trades only see a window).
    settings.raw["sources"]["binance_account"]["cost_basis"] = {"BTC": {"avg_price": 200.0}}
    upsert_trades(
        conn,
        pd.DataFrame(
            [
                {"trade_id": "1", "exchange": "binance", "symbol": "BTC/USDT", "side": "buy",
                 "ts": "2026-01-01T00:00:00+00:00", "price": 100.0, "amount": 0.5, "cost": 50.0,
                 "fee": 0.0, "fee_currency": "USDT"},
            ]
        ),
    )
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-23T00:00:00+00:00", 300.0),
                _obs("BTC:balance:spot", "2026-07-23T00:00:00+00:00", 0.5, source="binance"),
            ]
        ),
    )
    btc = wallet_pnl_table(conn, settings).query("symbol == 'BTC'").iloc[0]
    assert btc["source"] == "manual"
    assert btc["avg_price"] == pytest.approx(200.0)  # manual, not the trade-derived 100
    assert btc["pnl_pct"] == pytest.approx(50.0)  # 300/200 - 1


def test_wallet_pnl_manual_cost_basis(conn, settings) -> None:
    settings.raw["sources"]["binance_account"]["cost_basis"] = {
        "XRP": {"avg_price": 0.50},
        "WBETH": {"invested_usd": 100.0},
    }
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("ripple:price", "2026-07-23T00:00:00+00:00", 0.60),
                _obs("XRP:balance:spot", "2026-07-23T00:00:00+00:00", 100.0, source="binance"),
                _obs("wrapped-beacon-eth:price", "2026-07-23T00:00:00+00:00", 2000.0),
                _obs("WBETH:balance:spot", "2026-07-23T00:00:00+00:00", 0.05, source="binance"),
            ]
        ),
    )
    df = wallet_pnl_table(conn, settings)
    xrp = df.query("symbol == 'XRP'").iloc[0]
    assert xrp["source"] == "manual"
    assert xrp["pnl_pct"] == pytest.approx(20.0)  # 0.60/0.50 - 1
    wbeth = df.query("symbol == 'WBETH'").iloc[0]
    assert wbeth["avg_price"] == pytest.approx(2000.0)  # invested 100 / amount 0.05
    assert wbeth["pnl_pct"] == pytest.approx(0.0)


def test_wallet_value_history_holds_current_at_historical_prices(conn, settings) -> None:
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", "2026-07-20T00:00:00+00:00", 100.0),
                _obs("bitcoin:price", "2026-07-21T00:00:00+00:00", 200.0),
                _obs("BTC:balance:spot", "2026-07-23T00:00:00+00:00", 2.0, source="binance"),
                _obs("USDT:balance:spot", "2026-07-23T00:00:00+00:00", 50.0, source="binance"),
            ]
        ),
    )
    vh = wallet_value_history(conn, settings)  # 2 BTC * price + 50 cash
    assert len(vh) == 2
    assert vh.iloc[0] == pytest.approx(250.0)  # 2*100 + 50
    assert vh.iloc[-1] == pytest.approx(450.0)  # 2*200 + 50


def test_execution_summary_from_trades(conn, settings) -> None:
    upsert_trades(
        conn,
        pd.DataFrame(
            [
                {"trade_id": "binance:1", "exchange": "binance", "symbol": "BTC/USDT",
                 "side": "buy", "ts": "2026-07-20T00:00:00+00:00", "price": 65000.0,
                 "amount": 0.01, "cost": 650.0, "fee": 0.65, "fee_currency": "USDT"},
                {"trade_id": "binance:2", "exchange": "binance", "symbol": "ETH/USDT",
                 "side": "buy", "ts": "2026-07-21T00:00:00+00:00", "price": 1900.0,
                 "amount": 0.1, "cost": 190.0, "fee": 0.19, "fee_currency": "USDT"},
            ]
        ),
    )
    summary = execution_summary(conn, settings)
    assert summary["has_trades"] is True
    assert summary["n_trades"] == 2
    assert summary["invested_usd"] == pytest.approx(840.0)
    assert summary["net_invested_usd"] == pytest.approx(840.0)
    assert summary["fees_usd"] == pytest.approx(0.84)  # stablecoin fees at $1
    assert summary["fees_unconverted"] == 0


def test_execution_summary_no_trades(conn, settings) -> None:
    assert execution_summary(conn, settings) == {"has_trades": False}


def test_dca_status_empty_plan(conn, settings) -> None:
    status = dca_status(conn, settings)
    assert status["deployed_usd"] == 0.0
    assert status["planned_usd"] == 0.0
    assert status["next_tranche"] is None
    assert status["monthly_min_usd"] == 200

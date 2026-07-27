"""Tests for IDEAS_MEJORAS Parte A — new free data sources (CLAUDE.md section 10).

Covers the pure parsers (no network) and the DB-backed transforms (seeded temp SQLite):
    A1  Fed net liquidity (unit scaling + as-of alignment)
    A3  Binance futures/data top-L/S + taker ratio parser
    A4  perp-spot basis (relative_premium_pct + market_structure column)
    A5  Deribit DVOL parser
    A6  Fear & Greed parser
    A7  DefiLlama stablecoins parser
    A8  Coinbase premium transform
    A9  ETH/BTC + TOTAL2/TOTAL3 rotation transform
    A10 Blockchain.com on-chain parser
    A11 DefiLlama revenue history parser
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from core.config import load_settings
from db.loader import OBSERVATION_COLUMNS, init_db, upsert_observations
from transform.indicators import (
    _vote,
    coinbase_premium_summary,
    fed_net_liquidity_series,
    regime_scoreboard,
    relative_premium_pct,
    rotation_summary,
)
from transform.rally_quality import market_structure_table


@pytest.fixture()
def settings():
    load_settings.cache_clear()
    s = load_settings()
    yield s
    load_settings.cache_clear()


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "t.db")
    yield c
    c.close()


def _obs(series_id: str, ts: str, value: float, source: str) -> dict:
    return {"source": source, "series_id": series_id, "ts": ts, "ts_release": None, "value": value}


def _day(d: int, month: int = 7) -> str:
    return f"2026-{month:02d}-{d:02d}T00:00:00+00:00"


# --- Pure parsers ------------------------------------------------------------


def test_relative_premium_pct():
    assert relative_premium_pct(105.0, 100.0) == pytest.approx(5.0)
    assert relative_premium_pct(98.0, 100.0) == pytest.approx(-2.0)
    assert relative_premium_pct(100.0, 0.0) is None  # zero base
    assert relative_premium_pct(None, 100.0) is None


def test_parse_futures_ratio_top_and_taker():
    from ingest.derivatives import parse_futures_ratio

    top = [
        {"symbol": "BTCUSDT", "longShortRatio": "2.5", "timestamp": 1_700_000_000_000},
        {"symbol": "BTCUSDT", "longShortRatio": None, "timestamp": 1_700_086_400_000},  # skip
    ]
    rows = parse_futures_ratio(top, "BTC", "top_ls_account", "longShortRatio")
    assert len(rows) == 1
    assert rows[0]["series_id"] == "BTC:top_ls_account:binance"
    assert rows[0]["value"] == 2.5
    assert rows[0]["source"] == "derivatives"
    assert set(OBSERVATION_COLUMNS) <= set(rows[0])

    taker = [{"buySellRatio": "1.1", "timestamp": 1_700_000_000_000}]
    trows = parse_futures_ratio(taker, "ETH", "taker_ratio", "buySellRatio")
    assert trows[0]["series_id"] == "ETH:taker_ratio:binance"
    assert trows[0]["value"] == pytest.approx(1.1)


def test_parse_dvol_keeps_daily_close():
    from ingest.deribit import parse_dvol

    payload = {
        "result": {
            "data": [
                [1_700_000_000_000, 40.0, 41.0, 39.0, 40.5],
                [1_700_086_400_000, 40.5, 42.0, 40.0, 41.7],
            ]
        }
    }
    rows = parse_dvol(payload, "BTC")
    assert len(rows) == 2
    assert rows[0]["series_id"] == "deribit:dvol:btc"
    # close (index 4) is stored, not open/high/low.
    assert {r["value"] for r in rows} == {40.5, 41.7}
    assert set(OBSERVATION_COLUMNS) <= set(rows[0])


def test_parse_fear_greed():
    from ingest.sentiment import parse_fear_greed

    payload = {
        "data": [
            {"value": "30", "value_classification": "Fear", "timestamp": "1700000000"},
            {"value": None, "timestamp": "1700086400"},  # skip
        ]
    }
    rows = parse_fear_greed(payload)
    assert len(rows) == 1
    assert rows[0]["series_id"] == "sentiment:fear_greed"
    assert rows[0]["value"] == 30.0
    assert rows[0]["source"] == "alternative_me"


def test_parse_stablecoins_chart():
    from ingest.defillama import parse_stablecoins_chart

    payload = [
        {"date": "1700000000", "totalCirculatingUSD": {"peggedUSD": 150e9}},
        {"date": "1700086400", "totalCirculatingUSD": {"peggedUSD": 151e9}},
        {"date": "1700172800", "totalCirculatingUSD": {}},  # no peggedUSD -> skip
    ]
    rows = parse_stablecoins_chart(payload, history_days=400)
    assert len(rows) == 2
    assert all(r["series_id"] == "stablecoins:total_mcap" for r in rows)
    assert rows[-1]["value"] == pytest.approx(151e9)
    # history_days slice keeps the most recent
    assert parse_stablecoins_chart(payload, history_days=1)[0]["value"] == pytest.approx(151e9)


def test_parse_revenue_history():
    from ingest.defillama import parse_revenue_history

    payload = {"totalDataChart": [[1_700_000_000, 100.0], [1_700_086_400, 120.0], [1_700_172_800, None]]}
    rows = parse_revenue_history(payload, "aave", history_days=400)
    assert len(rows) == 2
    assert all(r["series_id"] == "aave:revenue" for r in rows)
    assert {r["value"] for r in rows} == {100.0, 120.0}


def test_parse_bcom_chart():
    from ingest.blockchain_com import parse_bcom_chart

    payload = {"values": [{"x": 1_700_000_000, "y": 5.0e8}, {"x": 1_700_086_400, "y": 5.1e8}]}
    rows = parse_bcom_chart(payload, "hash-rate")
    assert len(rows) == 2
    assert rows[0]["series_id"] == "btc:hash-rate"
    assert rows[-1]["value"] == pytest.approx(5.1e8)


# --- DB-backed transforms ----------------------------------------------------


def test_fed_net_liquidity_units_and_asof(conn, settings) -> None:
    """A1: WALCL/TGA in millions, RRP in billions; net = WALCL-TGA-RRP in billions."""
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                # WALCL (millions): 7.0M million = $7.0T -> 7000 billion
                _obs("WALCL", _day(1), 7_000_000.0, "fred"),
                _obs("WALCL", _day(8), 7_100_000.0, "fred"),
                # WTREGEN / TGA (millions): 820,000 million -> 820 billion
                _obs("WTREGEN", _day(1), 800_000.0, "fred"),
                _obs("WTREGEN", _day(8), 820_000.0, "fred"),
                # RRPONTSYD (billions): 500 -> 500 billion (daily, only on the 8th here)
                _obs("RRPONTSYD", _day(8), 500.0, "fred"),
            ]
        ),
    )
    s = fed_net_liquidity_series(conn, settings)
    # Only 2026-07-08 has all three components -> one aligned point.
    assert len(s) == 1
    # 7100 - 820 - 500 = 5780 (billions)
    assert float(s.iloc[-1]) == pytest.approx(5780.0)


def test_rotation_summary(conn, settings) -> None:
    """A9: ETH/BTC ratio and TOTAL2/TOTAL3 from own CoinGecko data."""
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("bitcoin:price", _day(8), 100_000.0, "coingecko"),
                _obs("ethereum:price", _day(8), 4_000.0, "coingecko"),
                _obs("global:total_market_cap", _day(8), 3.0e12, "coingecko"),
                _obs("bitcoin:market_cap", _day(8), 2.0e12, "coingecko"),
                _obs("ethereum:market_cap", _day(8), 5.0e11, "coingecko"),
            ]
        ),
    )
    r = rotation_summary(conn, settings)
    assert r["has_data"] is True
    assert r["eth_btc"]["latest"] == pytest.approx(0.04)
    assert r["total2"]["latest"] == pytest.approx(1.0e12)   # 3e12 - 2e12
    assert r["total3"]["latest"] == pytest.approx(5.0e11)   # 3e12 - 2e12 - 5e11


def test_coinbase_premium_summary(conn, settings) -> None:
    """A8: (coinbase - binance)/binance per asset, aligned as-of."""
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("BTC:spot_close:coinbase", _day(8), 100_500.0, "spot_prices"),
                _obs("BTC:spot_close:binance", _day(8), 100_000.0, "spot_prices"),
            ]
        ),
    )
    out = coinbase_premium_summary(conn, settings)
    assert out["has_data"] is True
    btc = next(p for p in out["per_asset"] if p["asset"] == "BTC")
    assert btc["premium_pct"] == pytest.approx(0.5)


def test_market_structure_basis_and_ratios(conn, settings) -> None:
    """A3/A4: market_structure_table exposes basis %, top L/S and taker ratio."""
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("BTC:close:binance", _day(8), 101_000.0, "derivatives"),  # perp
                _obs("bitcoin:price", _day(8), 100_000.0, "coingecko"),        # spot
                _obs("BTC:top_ls_account:binance", _day(8), 1.2, "derivatives"),
                _obs("BTC:taker_ratio:binance", _day(8), 1.05, "derivatives"),
            ]
        ),
    )
    ms = market_structure_table(conn, settings)
    btc = ms[ms["symbol"] == "BTC"].iloc[0]
    assert btc["basis_pct"] == pytest.approx(1.0)  # (101000/100000 - 1)*100
    assert btc["top_ls_account"] == pytest.approx(1.2)
    assert btc["taker_ratio"] == pytest.approx(1.05)


def test_regime_vote():
    """B1: dead-zone vote, both directions."""
    # direction +1: above high = risk-on, below low = risk-off.
    assert _vote(1.0, -0.5, 0.5) == 1
    assert _vote(-1.0, -0.5, 0.5) == -1
    assert _vote(0.0, -0.5, 0.5) == 0
    # direction -1 (a rise is risk-off, e.g. NFCI/DXY/HY).
    assert _vote(1.0, -0.5, 0.5, direction=-1) == -1
    assert _vote(-1.0, -0.5, 0.5, direction=-1) == 1


def test_regime_scoreboard_risk_off(conn, settings) -> None:
    """B1: NFCI restrictive + BTC ETF outflow streak -> score -2 -> risk_off."""
    upsert_observations(
        conn,
        pd.DataFrame(
            [_obs("NFCI", _day(8), 0.50, "fred")]  # >0.05 dead-zone, restrictive -> -1
            + [_obs("etf:btc:total", _day(d), -50.0, "farside") for d in (6, 7, 8)]  # 3d outflow -> -1
        ),
    )
    r = regime_scoreboard(conn, settings)
    assert r["has_data"] is True
    names = {c["name"]: c["vote"] for c in r["components"]}
    assert names["NFCI (condiciones fin.)"] == -1
    assert names["Flujos ETF BTC"] == -1
    assert r["score"] == sum(c["vote"] for c in r["components"])  # score is the transparent sum
    assert r["score"] <= r["risk_off_at"] and r["label"] == "risk_off"


def test_regime_scoreboard_risk_on(conn, settings) -> None:
    """B1: NFCI loose + BTC ETF inflow streak -> score +2 -> risk_on."""
    upsert_observations(
        conn,
        pd.DataFrame(
            [_obs("NFCI", _day(8), -0.50, "fred")]  # loose -> +1
            + [_obs("etf:btc:total", _day(d), 60.0, "farside") for d in (7, 8)]  # 2d inflow -> +1
        ),
    )
    r = regime_scoreboard(conn, settings)
    assert r["score"] >= r["risk_on_at"] and r["label"] == "risk_on"


def test_regime_scoreboard_neutral(conn, settings) -> None:
    """B1: a single in-dead-zone signal -> score 0 -> neutral."""
    upsert_observations(conn, pd.DataFrame([_obs("NFCI", _day(8), 0.0, "fred")]))
    r = regime_scoreboard(conn, settings)
    assert r["n"] == 1 and r["score"] == 0 and r["label"] == "neutral"


def test_parte_a_series_are_idempotent(conn, settings) -> None:
    """New series upsert idempotently (no duplicate rows on re-run)."""
    rows = pd.DataFrame(
        [
            _obs("stablecoins:total_mcap", _day(8), 150e9, "defillama"),
            _obs("deribit:dvol:btc", _day(8), 40.0, "deribit"),
            _obs("sentiment:fear_greed", _day(8), 30.0, "alternative_me"),
        ]
    )
    upsert_observations(conn, rows)
    upsert_observations(conn, rows)  # re-run
    n = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE series_id IN "
        "('stablecoins:total_mcap','deribit:dvol:btc','sentiment:fear_greed')"
    ).fetchone()[0]
    assert n == 3

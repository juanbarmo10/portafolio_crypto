"""Parser tests for the JSON ingesters (CLAUDE.md section 10: tests obligatorios en parsers).

These monkeypatch ``requests.get`` with captured-shape payloads so the JSON parsing
is exercised without the network. If CoinGecko or DefiLlama change their response
shape, these fail loudly (section 9) rather than the ingester silently emitting nothing.
"""

from __future__ import annotations

import ingest.coingecko as cg_mod
import ingest.defillama as dl_mod
from core.config import load_settings
from db.loader import OBSERVATION_COLUMNS
from ingest.coingecko import CoinGeckoIngester
from ingest.defillama import DefiLlamaIngester


class _FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _settings():
    load_settings.cache_clear()
    return load_settings()


def test_coingecko_parses_markets_and_global(monkeypatch) -> None:
    markets = [
        {
            "id": "bitcoin",
            "current_price": 65000.0,
            "market_cap": 1.3e12,
            "total_volume": 2.5e10,
            "circulating_supply": 19_700_000.0,
            "total_supply": 19_700_000.0,
            "max_supply": 21_000_000.0,
            "ath": 100_000.0,
        },
        {
            "id": "ethereum",
            "current_price": 1900.0,
            "market_cap": 2.3e11,
            "total_volume": 1.0e10,
            "circulating_supply": 120_000_000.0,
            "total_supply": 120_000_000.0,
            "max_supply": None,  # uncapped -> max_supply row must be skipped
            "ath": 4800.0,
        },
    ]
    glob = {"data": {"market_cap_percentage": {"btc": 56.5, "eth": 12.0},
                     "total_market_cap": {"usd": 2.3e12}}}

    def fake_get(url, **kwargs):
        return _FakeResponse(glob) if url.endswith("/global") else _FakeResponse(markets)

    monkeypatch.setattr(cg_mod.requests, "get", fake_get)

    df = CoinGeckoIngester(_settings()).fetch()
    assert list(df.columns) == OBSERVATION_COLUMNS
    ids = set(df["series_id"])
    assert "bitcoin:price" in ids
    assert "global:btc_dominance" in ids
    # ethereum max_supply is None -> no such row
    assert "ethereum:max_supply" not in ids
    btc_price = df.loc[df["series_id"] == "bitcoin:price", "value"].iloc[0]
    assert btc_price == 65000.0
    dom = df.loc[df["series_id"] == "global:btc_dominance", "value"].iloc[0]
    assert dom == 56.5


def test_defillama_parses_protocol_and_chain(monkeypatch) -> None:
    protocol_payload = {
        "tvl": [
            {"date": 1_700_000_000, "totalLiquidityUSD": 100.0},
            {"date": 1_700_086_400, "totalLiquidityUSD": 110.0},
            {"date": 1_700_172_800, "totalLiquidityUSD": None},  # dropped
        ]
    }
    chain_payload = [
        {"date": 1_700_000_000, "tvl": 500.0},
        {"date": 1_700_086_400, "tvl": 520.0},
    ]

    # Protocol fees/revenue summary: 30d snapshot + daily history (A11).
    revenue_payload = {
        "total30d": 3000.0,
        "totalDataChart": [[1_700_000_000, 90.0], [1_700_086_400, 100.0]],
    }
    # A7 aggregate stablecoins (different host).
    stables_payload = [
        {"date": "1700000000", "totalCirculatingUSD": {"peggedUSD": 150e9}},
        {"date": "1700086400", "totalCirculatingUSD": {"peggedUSD": 151e9}},
    ]

    def fake_get(url, **kwargs):
        if "stablecoincharts" in url:
            return _FakeResponse(stables_payload)
        if "/historicalChainTvl/" in url:
            return _FakeResponse(chain_payload)
        if "/summary/fees/" in url:
            return _FakeResponse(revenue_payload)  # protocol revenue snapshot + history
        return _FakeResponse(protocol_payload)

    monkeypatch.setattr(dl_mod.requests, "get", fake_get)

    df = DefiLlamaIngester(_settings()).fetch()
    assert list(df.columns) == OBSERVATION_COLUMNS
    # aave is a protocol in settings; its null-TVL point must be dropped.
    aave = df[df["series_id"] == "aave:tvl"]
    assert len(aave) == 2
    assert set(aave["value"]) == {100.0, 110.0}
    # Sui is a chain in settings.
    sui = df[df["series_id"] == "Sui:tvl"]
    assert len(sui) == 2
    assert 520.0 in set(sui["value"])
    # Revenue snapshot is emitted for protocol slugs (not chains).
    aave_rev = df[df["series_id"] == "aave:revenue_30d"]
    assert len(aave_rev) == 1 and set(aave_rev["value"]) == {3000.0}
    assert df[df["series_id"] == "Sui:revenue_30d"].empty  # chains have no fees entry
    # A11: revenue daily history is stored per protocol slug.
    aave_rev_hist = df[df["series_id"] == "aave:revenue"]
    assert len(aave_rev_hist) == 2 and set(aave_rev_hist["value"]) == {90.0, 100.0}
    # A7: aggregate stablecoin market cap.
    stables = df[df["series_id"] == "stablecoins:total_mcap"]
    assert len(stables) == 2 and 151e9 in set(stables["value"])
    load_settings.cache_clear()


def test_parse_market_chart_dedups_by_day():
    """market_chart -> one daily price observation per UTC day (last price wins)."""
    from datetime import datetime, timezone

    from ingest.coingecko import parse_market_chart

    def ms(y, mo, d, h=0):
        return int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp() * 1000)

    payload = {
        "prices": [
            [ms(2026, 1, 29, 0), 100.0],
            [ms(2026, 1, 29, 12), 110.0],  # same UTC day -> last wins (110)
            [ms(2026, 1, 30, 0), 120.0],
            [ms(2026, 1, 31, 0), None],  # null price -> skipped
        ]
    }
    out = parse_market_chart(payload, "bitcoin")
    assert len(out) == 2
    d29 = next(o for o in out if o["ts"].startswith("2026-01-29"))
    assert d29["value"] == 110.0
    assert d29["series_id"] == "bitcoin:price"
    assert d29["ts"] == "2026-01-29T00:00:00+00:00"
    assert d29["source"] == "coingecko"
    assert set(OBSERVATION_COLUMNS) <= set(d29)


def test_parse_long_short():
    """Binance long/short account ratio -> daily observation rows (pure)."""
    from ingest.derivatives import parse_long_short

    records = [
        {"symbol": "BTCUSDT", "longShortRatio": "1.85", "timestamp": 1_700_000_000_000},
        {"symbol": "BTCUSDT", "longShortRatio": None, "timestamp": 1_700_086_400_000},  # skip
    ]
    rows = parse_long_short(records, "BTC")
    assert len(rows) == 1
    assert rows[0]["series_id"] == "BTC:long_short:binance"
    assert rows[0]["value"] == 1.85
    assert rows[0]["source"] == "derivatives"
    assert set(OBSERVATION_COLUMNS) <= set(rows[0])

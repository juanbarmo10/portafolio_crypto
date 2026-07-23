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

    def __init__(self, payload):
        self._payload = payload

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

    def fake_get(url, **kwargs):
        if "/historicalChainTvl/" in url:
            return _FakeResponse(chain_payload)
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
    load_settings.cache_clear()

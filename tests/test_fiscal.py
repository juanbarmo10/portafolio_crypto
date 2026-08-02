"""Tests for the Colombian tax layer (FISCAL.md §10). The sensitive ones:

- COP cost is frozen at the acquisition-day TRM (Art. 269).
- PEPS consumes the oldest lot first; regime splits by 24-month maturity.
- A USD loss can be a taxable COP gain when the peso devalues.

Self-contained: fiscal config is injected so the suite passes with or without the
gitignored settings.local.yaml (CI has none).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from core.config import load_settings
from db.loader import init_db, upsert_observations, upsert_trades
from transform.fiscal import (
    build_tax_lots,
    cop_usd_pnl_table,
    maturity_summary,
    simulate_peps_sale,
)


@pytest.fixture()
def settings():
    load_settings.cache_clear()
    s = load_settings()
    ba = s.raw.get("sources", {}).get("binance_account", {})
    ba["manual_holdings"] = []
    ba["cost_basis"] = {}
    # Inject deterministic fiscal config (independent of any local override).
    s.raw["fiscal"] = {
        "enabled": True,
        "long_term_months": 24,
        "trm": {"source": "banrep", "series_id": "TRM:COP_USD"},
    }
    yield s
    load_settings.cache_clear()


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = init_db(tmp_path / "t.db")
    yield connection
    connection.close()


def _trm(ts: str, value: float) -> dict:
    return {"source": "banrep", "series_id": "TRM:COP_USD", "ts": ts, "ts_release": ts, "value": value}


def _price(cid: str, ts: str, value: float) -> dict:
    return {"source": "coingecko", "series_id": f"{cid}:price", "ts": ts, "ts_release": None, "value": value}


def _trade(trade_id: str, side: str, ts: str, amount: float, price: float, cost: float,
           symbol: str = "BTC/USDT") -> dict:
    return {
        "trade_id": trade_id, "exchange": "binance", "symbol": symbol, "side": side,
        "ts": ts, "price": price, "amount": amount, "cost": cost, "fee": 0.0, "fee_currency": "USDT",
    }


def test_build_lots_cop_freeze(conn, settings) -> None:
    upsert_observations(conn, pd.DataFrame([_trm("2025-07-16T00:00:00+00:00", 4000.0)]))
    upsert_trades(conn, pd.DataFrame([_trade("b1", "buy", "2025-07-16T00:00:00+00:00", 0.01, 60000.0, 600.0)]))
    lots = build_tax_lots(conn, settings)["lots"]
    assert len(lots) == 1
    lot = lots[0]
    assert lot["trm_acquisition"] == pytest.approx(4000.0)
    assert lot["cost_cop"] == pytest.approx(600.0 * 4000.0)   # frozen at acquisition-day TRM
    assert lot["matures_at"][:10] == "2027-07-16"             # +24 months
    assert lot["origin"] == "compra"


def test_peps_consumes_oldest_first(conn, settings) -> None:
    upsert_observations(conn, pd.DataFrame([
        _trm("2025-01-01T00:00:00+00:00", 4000.0),
        _trm("2025-06-01T00:00:00+00:00", 4100.0),
        _trm("2025-07-01T00:00:00+00:00", 4200.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("old", "buy", "2025-01-01T00:00:00+00:00", 0.01, 60000.0, 600.0),
        _trade("new", "buy", "2025-06-01T00:00:00+00:00", 0.01, 62000.0, 620.0),
        _trade("s1", "sell", "2025-07-01T00:00:00+00:00", 0.005, 63000.0, 315.0),
    ]))
    built = build_tax_lots(conn, settings)
    assert len(built["disposals"]) == 1
    cons = built["consumption"]
    assert len(cons) == 1
    assert cons[0]["lot_id"] == "old"          # oldest consumed first (PEPS)
    assert cons[0]["units"] == pytest.approx(0.005)


def test_peps_regime_split_by_maturity(conn, settings) -> None:
    upsert_observations(conn, pd.DataFrame([
        _trm("2023-01-01T00:00:00+00:00", 4000.0),
        _trm("2025-06-01T00:00:00+00:00", 4100.0),
        _trm("2025-07-01T00:00:00+00:00", 4200.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("old", "buy", "2023-01-01T00:00:00+00:00", 0.01, 30000.0, 300.0),   # >24m at sale
        _trade("new", "buy", "2025-06-01T00:00:00+00:00", 0.01, 62000.0, 620.0),   # <24m at sale
        _trade("s1", "sell", "2025-07-01T00:00:00+00:00", 0.015, 63000.0, 945.0),  # eats old + part new
    ]))
    cons = {c["lot_id"]: c for c in build_tax_lots(conn, settings)["consumption"]}
    assert cons["old"]["regime"] == "ganancia_ocasional"   # held >= 24 months
    assert cons["new"]["regime"] == "renta_ordinaria"       # held < 24 months


def test_cop_usd_divergence(conn, settings) -> None:
    # Peso devalues (TRM 4000 -> 4800, +20%); BTC falls 10% in USD. USD = loss, COP = gain.
    upsert_observations(conn, pd.DataFrame([
        _trm("2025-01-01T00:00:00+00:00", 4000.0),
        _trm("2026-01-01T00:00:00+00:00", 4800.0),   # latest TRM
        _price("bitcoin", "2026-01-01T00:00:00+00:00", 90000.0),  # fell from 100k
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", "2025-01-01T00:00:00+00:00", 0.001, 100000.0, 100.0),
    ]))
    df = cop_usd_pnl_table(conn, settings)
    row = df[df["asset"] == "BTC"].iloc[0]
    assert row["pnl_usd"] < 0     # 90 - 100 = -10 in USD
    assert row["pnl_cop"] > 0     # 432,000 - 400,000 = +32,000 in COP
    assert df.attrs["patrimonio_cop"] == pytest.approx(100.0 * 4000.0)  # net worth at COST


# --- Fase B: maturation + PEPS sale simulator -------------------------------


def _two_lots(conn) -> None:
    """One matured lot (bought 2023) + one recent lot (2025), with TRM and a current price."""
    upsert_observations(conn, pd.DataFrame([
        _trm("2023-01-01T00:00:00+00:00", 4000.0),
        _trm("2025-06-01T00:00:00+00:00", 4100.0),
        _trm("2026-08-02T00:00:00+00:00", 5000.0),           # latest TRM
        _price("bitcoin", "2026-08-02T00:00:00+00:00", 50000.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("old", "buy", "2023-01-01T00:00:00+00:00", 0.01, 30000.0, 300.0),  # matured
        _trade("new", "buy", "2025-06-01T00:00:00+00:00", 0.01, 62000.0, 620.0),  # not matured
    ]))


def test_maturity_summary_units_and_next(conn, settings) -> None:
    _two_lots(conn)
    row = maturity_summary(conn, settings).iloc[0]
    assert row["units_open"] == pytest.approx(0.02)
    assert row["units_matured"] == pytest.approx(0.01)     # only the 2023 lot has matured
    assert row["pct_matured"] == pytest.approx(50.0)
    assert row["next_maturity"][:10] == "2027-06-01"       # the 2025 lot's maturity
    assert row["days_to_next"] > 0


def test_simulate_peps_regime_split_and_tax(conn, settings) -> None:
    _two_lots(conn)
    # Sell 0.015: consumes the whole matured lot (0.01) + half the recent lot (0.005).
    sim = simulate_peps_sale(conn, settings, "BTC", 0.015)
    assert sim["has_data"] is True
    assert sim["shortfall"] == pytest.approx(0.0)
    assert len(sim["consumed"]) == 2
    # Matured lot: proceeds 0.01*50000*5000=2.5M, cost 300*4000=1.2M -> +1.3M occasional.
    assert sim["occasional_gain_cop"] == pytest.approx(1_300_000.0)
    assert sim["tax_occasional_cop"] == pytest.approx(1_300_000.0 * 0.15)  # 15%
    # Ordinary marginal rate unset (0) -> tax not estimable, reported as None (honest).
    assert sim["tax_ordinary_cop"] is None
    regimes = {c["lot_id"]: c["regime"] for c in sim["consumed"]}
    assert regimes["old"] == "ganancia_ocasional"
    assert regimes["new"] == "renta_ordinaria"


def test_simulate_shortfall_beyond_lots(conn, settings) -> None:
    _two_lots(conn)  # 0.02 BTC lotted
    sim = simulate_peps_sale(conn, settings, "BTC", 0.05)  # ask more than we hold
    assert sim["sold_units"] == pytest.approx(0.02)
    assert sim["shortfall"] == pytest.approx(0.03)


def test_simulate_no_price_no_data(conn, settings) -> None:
    # TRM + a lot but NO current price -> cannot value the sale.
    upsert_observations(conn, pd.DataFrame([_trm("2025-01-01T00:00:00+00:00", 4000.0)]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", "2025-01-01T00:00:00+00:00", 0.01, 60000.0, 600.0),
    ]))
    assert simulate_peps_sale(conn, settings, "BTC", 0.01)["has_data"] is False

"""Tests for the Colombian tax layer (RESEARCH.md §17). The sensitive ones:

- COP cost is frozen at the acquisition-day TRM (Art. 269).
- PEPS consumes the oldest lot first; regime splits by 24-month maturity.
- A USD loss can be a taxable COP gain when the peso devalues.

Self-contained: fiscal config is injected so the suite passes with or without the
gitignored settings.local.yaml (CI has none).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from core.config import load_settings
from db.loader import init_db, upsert_observations, upsert_trades
from alerts.rules import _lot_maturity_soon
from transform.fiscal import (
    art241_tax_cop,
    build_tax_lots,
    cop_usd_pnl_table,
    disposal_summary,
    estimate_ordinary_tax,
    exit_ladder_table,
    form160_check,
    maturity_summary,
    persist_thesis_and_ladder,
    pnl_attribution_cop,
    short_term_disposals,
    simulate_peps_sale,
    thesis_log_table,
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


def test_pnl_attribution_reconciles(conn, settings) -> None:
    # Peso devalued (TRM 4000->4800) while BTC fell 100->90 USD: crypto loss + FX gain = pnl_cop.
    upsert_observations(conn, pd.DataFrame([
        _trm("2025-01-01T00:00:00+00:00", 4000.0),
        _trm("2026-01-01T00:00:00+00:00", 4800.0),
        _price("bitcoin", "2026-01-01T00:00:00+00:00", 90000.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", "2025-01-01T00:00:00+00:00", 0.001, 100000.0, 100.0),
    ]))
    att = pnl_attribution_cop(conn, settings)
    assert att["has_data"]
    assert att["trm_cost_avg"] == pytest.approx(4000.0)
    assert att["crypto_cop"] == pytest.approx(-40_000.0)  # (90-100)*4000 (price move at buy TRM)
    assert att["fx_cop"] == pytest.approx(72_000.0)       # 90*(4800-4000) (rate move on position)
    assert att["total_cop"] == pytest.approx(32_000.0)    # reconciles to value_cop - cost_cop


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


# --- Fase C: disposal counter (habitual-activity risk) ----------------------


def test_disposal_summary_counts_and_habitual_flag(conn, settings) -> None:
    settings.raw["fiscal"]["habituality"] = {"warn_disposals_per_year": 2}
    upsert_observations(conn, pd.DataFrame([
        _trm("2026-01-01T00:00:00+00:00", 4000.0),
        _trm("2026-03-01T00:00:00+00:00", 4100.0),
        _trm("2026-05-01T00:00:00+00:00", 4200.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", "2026-01-01T00:00:00+00:00", 0.03, 60000.0, 1800.0),
        _trade("s1", "sell", "2026-03-01T00:00:00+00:00", 0.01, 61000.0, 610.0),
        _trade("s2", "sell", "2026-05-01T00:00:00+00:00", 0.01, 62000.0, 620.0),
    ]))
    d = disposal_summary(conn, settings)
    assert d["count"] == 2           # two sells this year; buys don't count
    assert d["ventas"] == 2
    assert d["threshold"] == 2
    assert d["habitual_risk"] is True   # count >= threshold


def test_disposal_summary_no_disposals(conn, settings) -> None:
    upsert_observations(conn, pd.DataFrame([_trm("2026-01-01T00:00:00+00:00", 4000.0)]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", "2026-01-01T00:00:00+00:00", 0.01, 60000.0, 600.0),
    ]))
    d = disposal_summary(conn, settings)
    assert d["count"] == 0
    assert d["habitual_risk"] is False


# --- Fase D: thesis journal + exit ladder -----------------------------------


def test_thesis_log_table_overdue_flag(conn, settings) -> None:
    settings.raw["fiscal"]["thesis_log"] = [{
        "thesis_id": "t_btc", "asset": "BTC", "written_at": "2026-01-01",
        "thesis": "reserva de valor", "falsification_criteria": "flujos ETF negativos",
        "status": "vigente", "review_date": "2026-01-15",  # past -> overdue
    }]
    persist_thesis_and_ladder(conn, settings)
    row = thesis_log_table(conn, settings).iloc[0]
    assert row["asset"] == "BTC"
    assert bool(row["review_overdue"]) is True   # past review date, still 'vigente'


def test_exit_ladder_price_progress(conn, settings) -> None:
    upsert_observations(conn, pd.DataFrame([
        _trm("2026-08-02T00:00:00+00:00", 4000.0),
        _price("bitcoin", "2026-08-02T00:00:00+00:00", 75000.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", "2026-01-01T00:00:00+00:00", 0.01, 60000.0, 600.0),
    ]))
    settings.raw["fiscal"]["exit_ladder"] = [
        {"ladder_id": "p1", "asset": "BTC", "tranche_n": 1, "trigger_type": "precio",
         "trigger_value": 150000.0, "pct_to_sell": 30.0},
    ]
    persist_thesis_and_ladder(conn, settings)
    row = exit_ladder_table(conn, settings).iloc[0]
    assert row["progress_pct"] == pytest.approx(75000.0 / 150000.0 * 100.0)  # 50%
    assert bool(row["reached"]) is False


def test_form160_no_uvt_not_calculable(conn, settings) -> None:
    upsert_observations(conn, pd.DataFrame([
        _trm("2025-01-01T00:00:00+00:00", 4000.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", "2025-01-01T00:00:00+00:00", 0.01, 60000.0, 600.0),
    ]))
    f = form160_check(conn, settings)     # no uvt_cop in injected config
    assert f["has_uvt"] is False
    assert f["obligado"] is None
    assert f["patrimonio_cop"] == pytest.approx(600.0 * 4000.0)


def test_form160_obligado_with_uvt(conn, settings) -> None:
    from datetime import datetime, timezone
    year = datetime.now(timezone.utc).year
    settings.raw["fiscal"]["uvt_cop"] = {year: 49_800.0}     # threshold = 2000 * 49_800
    settings.raw["fiscal"]["foreign_assets_form160_uvt"] = 2000
    upsert_observations(conn, pd.DataFrame([_trm("2025-01-01T00:00:00+00:00", 4000.0)]))
    upsert_trades(conn, pd.DataFrame([
        # cost_cop = 600 * 4000 = 2.4M < 2000*49800 = 99.6M -> NOT obligated
        _trade("b1", "buy", "2025-01-01T00:00:00+00:00", 0.01, 60000.0, 600.0),
    ]))
    f = form160_check(conn, settings)
    assert f["has_uvt"] is True
    assert f["threshold_cop"] == pytest.approx(2000 * 49_800.0)
    assert f["obligado"] is False


def test_lot_maturity_soon_alert_fires(conn, settings) -> None:
    settings.raw["fiscal"]["enabled"] = True
    upsert_observations(conn, pd.DataFrame([_trm("2024-09-01T00:00:00+00:00", 4000.0)]))
    upsert_trades(conn, pd.DataFrame([
        # bought 2024-09-01 -> matures 2026-09-01 (~a month out from 'today' 2026-08-02)
        _trade("b1", "buy", "2024-09-01T00:00:00+00:00", 0.01, 30000.0, 300.0),
    ]))
    alerts = _lot_maturity_soon(conn, settings, {"within_days": 60})
    assert len(alerts) == 1
    assert alerts[0].rule_id == "lot_maturity_soon"
    assert "BTC" in alerts[0].message


def test_build_tax_lots_records_unmatched_sell(conn, settings) -> None:
    # Sell more than is lotted (rest acquired via Earn/Convert, not in `trades`): the excess is
    # recorded as `unmatched`, never dropped silently (RESEARCH.md §17/§2.3).
    upsert_observations(conn, pd.DataFrame([
        _trm("2025-01-01T00:00:00+00:00", 4000.0),
        _trm("2025-06-01T00:00:00+00:00", 4200.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", "2025-01-01T00:00:00+00:00", 0.01, 60000.0, 600.0),
        _trade("s1", "sell", "2025-06-01T00:00:00+00:00", 0.02, 63000.0, 1260.0),  # 0.02 > 0.01
    ]))
    built = build_tax_lots(conn, settings)
    assert built["disposals"][0]["unmatched"] == pytest.approx(0.01)   # 0.02 sold − 0.01 lotted
    d = disposal_summary(conn, settings, built=built)
    assert len(d["unmatched_events"]) == 1
    assert d["unmatched_events"][0]["units"] == pytest.approx(0.01)
    assert d["unmatched_events"][0]["asset"] == "BTC"


# --- short_term_disposals: the behavioral mirror (INVERSOR_IDEAS §2.1) -----------------------


def test_short_term_disposals_flags_recent_sell(conn, settings) -> None:
    # Bought and sold within days, THIS year: renta ordinaria, holding_days measured lot-by-lot.
    year = datetime.now(timezone.utc).year
    upsert_observations(conn, pd.DataFrame([
        _trm(f"{year}-07-28T00:00:00+00:00", 4000.0),
        _trm(f"{year}-07-30T00:00:00+00:00", 4000.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("b1", "buy", f"{year}-07-28T00:00:00+00:00", 0.01, 60000.0, 600.0),
        _trade("s1", "sell", f"{year}-07-30T00:00:00+00:00", 0.01, 63000.0, 630.0),
    ]))
    out = short_term_disposals(conn, settings)
    assert out["count"] == 1
    assert out["last"]["asset"] == "BTC"
    assert out["last"]["holding_days"] == 2                    # 28 -> 30 July
    assert out["last"]["gain_cop"] == pytest.approx((630.0 - 600.0) * 4000.0)  # +120,000 COP
    assert out["gain_cop"] == pytest.approx(120_000.0)


def test_short_term_disposals_ignores_matured(conn, settings) -> None:
    # A matured lot (>24m) sold long-term is ganancia ocasional, NOT a short-term flag.
    year = datetime.now(timezone.utc).year
    upsert_observations(conn, pd.DataFrame([
        _trm("2023-01-01T00:00:00+00:00", 4000.0),
        _trm(f"{year}-07-30T00:00:00+00:00", 4000.0),
    ]))
    upsert_trades(conn, pd.DataFrame([
        _trade("old", "buy", "2023-01-01T00:00:00+00:00", 0.01, 30000.0, 300.0),
        _trade("s1", "sell", f"{year}-07-30T00:00:00+00:00", 0.01, 63000.0, 630.0),
    ]))
    out = short_term_disposals(conn, settings)
    assert out["count"] == 0
    assert out["last"] is None


# --- Renta ordinaria progresiva (Art. 241) — estimador del impuesto <24m ---------------------


def test_art241_tax_cop_brackets() -> None:
    # UVT = 100 for round numbers. Below the 1090-UVT floor -> 0.
    assert art241_tax_cop(500 * 100, 100) == pytest.approx(0.0)
    assert art241_tax_cop(1090 * 100, 100) == pytest.approx(0.0)   # at the floor
    # 2000 UVT: band [1700,4100) 28%, accumulated 116 UVT -> (2000-1700)*.28 + 116 = 200 UVT.
    assert art241_tax_cop(2000 * 100, 100) == pytest.approx(200 * 100)
    # 5000 UVT: band [4100,8670) 33%, accumulated 788 -> (5000-4100)*.33 + 788 = 1085 UVT.
    assert art241_tax_cop(5000 * 100, 100) == pytest.approx(1085 * 100)
    assert art241_tax_cop(0, 100) == 0.0 and art241_tax_cop(1000, 0) == 0.0


def test_estimate_ordinary_tax_marginal_impact(settings) -> None:
    # Income 100M at UVT 50k -> 2000 UVT -> 28% band. A gain within the band taxes at 28% marginal.
    settings.raw["fiscal"]["uvt_cop"] = 50000
    settings.raw["fiscal"]["annual_ordinary_income_cop"] = 100_000_000
    e = estimate_ordinary_tax(settings, 1_000_000)
    assert e["has_config"]
    assert e["income_uvt"] == pytest.approx(2000.0)
    assert e["marginal_rate_pct"] == pytest.approx(28.0)
    assert e["tax_cop"] == pytest.approx(280_000.0)          # 28% of 1M (same band)
    # Negative/zero gain -> no tax.
    assert estimate_ordinary_tax(settings, -5000)["tax_cop"] == pytest.approx(0.0)


def test_estimate_ordinary_tax_uvt_year_map_and_unconfigured(settings) -> None:
    # uvt_cop as a {year: value} map resolves to the current year (2026).
    settings.raw["fiscal"]["uvt_cop"] = {2025: 49799, 2026: 52374}
    settings.raw["fiscal"]["annual_ordinary_income_cop"] = 100_000_000
    assert estimate_ordinary_tax(settings, 0)["uvt_cop"] == pytest.approx(52374.0)
    # Without an income, the progressive estimate can't be made.
    del settings.raw["fiscal"]["annual_ordinary_income_cop"]
    assert estimate_ordinary_tax(settings, 65_288)["has_config"] is False

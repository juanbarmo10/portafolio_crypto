"""Tests for transform.portfolio_risk (IDEAS_MEJORAS Parte B, B2/B3; CLAUDE.md section 10).

Seeds a controlled scenario where ETH returns are exactly 2× BTC returns, so correlation,
beta, HHI and risk contribution have known closed-form values — no network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from core.config import load_settings
from db.loader import init_db, upsert_observations
from transform.portfolio_risk import (
    _max_drawdown,
    correlation_matrix,
    portfolio_risk_summary,
)


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


def _price(cid: str, day: int, value: float) -> dict:
    return {
        "source": "coingecko",
        "series_id": f"{cid}:price",
        "ts": f"2026-07-{day:02d}T00:00:00+00:00",
        "ts_release": None,
        "value": value,
    }


def _seed_correlated(conn) -> pd.DataFrame:
    """BTC returns [+.1,-.1,+.1]; ETH returns exactly 2x -> corr 1, beta(ETH)=2."""
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _price("bitcoin", 24, 100.0),
                _price("bitcoin", 25, 110.0),   # +10%
                _price("bitcoin", 26, 99.0),    # -10%
                _price("bitcoin", 27, 108.9),   # +10%
                _price("ethereum", 24, 100.0),
                _price("ethereum", 25, 120.0),  # +20%
                _price("ethereum", 26, 96.0),   # -20%
                _price("ethereum", 27, 115.2),  # +20%
            ]
        ),
    )
    return pd.DataFrame(
        [
            {"asset": "BTC", "amount": 0.918, "value_usd": 100.0, "is_cash": False},
            {"asset": "ETH", "amount": 0.868, "value_usd": 100.0, "is_cash": False},
        ]
    )


def test_correlation_matrix(conn, settings) -> None:
    holdings = _seed_correlated(conn)
    cm = correlation_matrix(conn, settings, holdings)
    assert set(cm.columns) == {"BTC", "ETH"}
    assert cm.loc["BTC", "ETH"] == pytest.approx(1.0)  # ETH = 2x BTC -> perfectly correlated


def test_beta_hhi_and_contribution(conn, settings) -> None:
    holdings = _seed_correlated(conn)
    r = portfolio_risk_summary(conn, settings, holdings)
    assert r["has_data"] is True and r["n_assets"] == 2
    beta = {p["symbol"]: p["beta_btc"] for p in r["per_asset"]}
    assert beta["BTC"] == pytest.approx(1.0)
    assert beta["ETH"] == pytest.approx(2.0)          # ETH moves 2x BTC
    assert r["port_beta_btc"] == pytest.approx(1.5)    # equal weights: 0.5*1 + 0.5*2
    assert r["hhi"] == pytest.approx(0.5)              # 0.5^2 + 0.5^2
    assert r["effective_n"] == pytest.approx(2.0)      # 1/HHI
    assert r["avg_correlation"] == pytest.approx(1.0)
    # ETH is twice as volatile -> it carries the larger share of portfolio risk.
    rc = {p["symbol"]: p["risk_contribution_pct"] for p in r["per_asset"]}
    assert rc["ETH"] > rc["BTC"]
    assert rc["BTC"] + rc["ETH"] == pytest.approx(100.0)  # contributions sum to 100%
    assert ("BTC", "ETH", pytest.approx(1.0)) in [(a, b, c) for a, b, c in r["high_corr_pairs"]]


def test_max_drawdown_pure() -> None:
    idx = pd.date_range("2026-07-01", periods=3, freq="D", tz="UTC")
    assert _max_drawdown(pd.Series([100.0, 80.0, 120.0], index=idx)) == pytest.approx(-20.0)
    assert _max_drawdown(pd.Series([100.0], index=idx[:1])) is None  # too short


def test_risk_summary_no_holdings(conn, settings) -> None:
    assert portfolio_risk_summary(conn, settings, pd.DataFrame()).get("has_data") is False

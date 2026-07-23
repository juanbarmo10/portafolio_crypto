"""Tests for the alert rule engine (CLAUDE.md sections 8, 10).

Seeds the DB with each condition and asserts the evaluator fires, plus dedup and the
Telegram dry-run path. No network / no Telegram bot required.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from alerts.rules import (
    _etf_outflow_streak,
    _funding_crowded,
    _tvl_drop,
    _unlock_soon,
    dispatch_alerts,
)
from alerts.telegram import TelegramSender
from core.config import load_settings
from db.loader import init_db, upsert_observations


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


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message: str) -> bool:
        self.sent.append(message)
        return True


def test_tvl_drop_fires(conn, settings) -> None:
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("aave:tvl", "2026-07-16T00:00:00+00:00", 100.0, "defillama"),
                _obs("aave:tvl", "2026-07-23T00:00:00+00:00", 75.0, "defillama"),  # -25% in 7d
            ]
        ),
    )
    alerts = _tvl_drop(conn, settings, {"pct": 20})
    assert any("AAVE" in a.message for a in alerts)


def test_etf_outflow_streak_fires(conn, settings) -> None:
    rows = [
        _obs("etf:btc:total", f"2026-07-{d}T00:00:00+00:00", -50.0, "farside")
        for d in ("21", "22", "23")
    ]
    upsert_observations(conn, pd.DataFrame(rows))
    alerts = _etf_outflow_streak(conn, settings, {"min_days": 3})
    assert any("BTC" in a.message for a in alerts)


def test_funding_crowded_fires(conn, settings) -> None:
    idx = pd.date_range(end="2026-07-23", periods=20, freq="8h", tz="UTC")
    rows = [
        _obs("BTC:funding:binance", t.strftime("%Y-%m-%dT%H:%M:%S+00:00"), 0.0001, "derivatives")
        for t in idx[:-1]
    ]
    rows.append(
        _obs("BTC:funding:binance", idx[-1].strftime("%Y-%m-%dT%H:%M:%S+00:00"), 0.02, "derivatives")
    )
    upsert_observations(conn, pd.DataFrame(rows))
    alerts = _funding_crowded(conn, settings, {"z_threshold": 2.0})
    assert any("BTC" in a.message for a in alerts)


def test_unlock_soon_fires(conn, settings) -> None:
    soon = (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()
    settings.asset_meta["SUI"] = {**settings.asset_meta.get("SUI", {}), "next_unlock": soon}
    alerts = _unlock_soon(conn, settings, {"within_days": 7})
    assert any("SUI" in a.message for a in alerts)


def test_dispatch_dedup(conn, settings) -> None:
    upsert_observations(
        conn,
        pd.DataFrame(
            [
                _obs("aave:tvl", "2026-07-16T00:00:00+00:00", 100.0, "defillama"),
                _obs("aave:tvl", "2026-07-23T00:00:00+00:00", 75.0, "defillama"),
            ]
        ),
    )
    sender = _FakeSender()
    _, sent1 = dispatch_alerts(conn, settings, sender)
    assert sent1 >= 1
    first_count = len(sender.sent)
    _, sent2 = dispatch_alerts(conn, settings, sender)  # everything already logged
    assert sent2 == 0
    assert len(sender.sent) == first_count


def test_telegram_dry_run(settings) -> None:
    settings.secrets.pop("TELEGRAM_TOKEN", None)
    settings.secrets.pop("TELEGRAM_CHAT_ID", None)
    sender = TelegramSender(settings)
    assert sender.enabled is False
    assert sender.send("hola") is False

"""Derived indicators (CLAUDE.md sections 2, 5, 8).

Two layers:

1. Pure numeric functions (``btc_dominance``, ``pct_change_over_days``,
   ``distance_to_ath``, ``mc_tvl_ratio``, ``dilution_ratio``). No I/O, fully
   unit-testable — this is where section 10's "tests obligatorios en
   transformaciones" is satisfied.
2. Table builders (``macro_table``, ``portfolio_table``, ``thesis_tvl_table``,
   ``dca_status``) that read stored observations via :mod:`db.queries` and assemble
   the DataFrames the dashboard renders. They perform no network I/O.

All percentage helpers return values in percent (e.g. -12.5 for -12.5%). Missing
inputs yield ``None`` rather than raising, so a cold-start DB shows blanks, not errors.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

import pandas as pd

from core.config import Settings
from db.queries import latest_by_source, latest_observation, series_history

# ---------------------------------------------------------------------------
# Pure numeric helpers
# ---------------------------------------------------------------------------


def btc_dominance(market_caps: Mapping[str, float], btc_key: str = "bitcoin") -> float | None:
    """Return BTC's share of total market cap, in percent.

    Args:
        market_caps: Mapping of asset id -> market cap (same currency).
        btc_key: Key identifying Bitcoin in the mapping.

    Returns:
        BTC dominance in percent, or None if BTC is absent or the total is zero.
    """
    total = sum(v for v in market_caps.values() if v is not None)
    btc = market_caps.get(btc_key)
    if btc is None or total <= 0:
        return None
    return btc / total * 100.0


def pct_change_over_days(history: pd.Series, days: int) -> float | None:
    """Percent change of a daily series over the last ``days`` calendar days.

    Compares the latest value against the last value on or before
    ``latest_date - days`` (as-of lookup, tolerant of gaps/weekends).

    Args:
        history: Series indexed by tz-aware datetimes, ascending.
        days: Look-back window in days.

    Returns:
        Percent change, or None if there is insufficient history or the past
        reference value is zero/missing.
    """
    hist = history.dropna()
    if hist.empty:
        return None
    latest_ts = hist.index[-1]
    latest_val = hist.iloc[-1]
    cutoff = latest_ts - pd.Timedelta(days=days)
    past = hist.loc[hist.index <= cutoff]
    if past.empty:
        return None
    past_val = past.iloc[-1]
    if past_val == 0:
        return None
    return (latest_val / past_val - 1.0) * 100.0


def abs_change_over_days(history: pd.Series, days: int) -> float | None:
    """Absolute change of a daily series over the last ``days`` (latest minus past).

    Like :func:`pct_change_over_days` but returns the raw difference — the right unit
    for quantities already expressed in percent (e.g. BTC dominance, in percentage
    points). Uses an as-of lookup tolerant of gaps. None if history is insufficient.
    """
    hist = history.dropna()
    if hist.empty:
        return None
    latest_ts = hist.index[-1]
    latest_val = hist.iloc[-1]
    cutoff = latest_ts - pd.Timedelta(days=days)
    past = hist.loc[hist.index <= cutoff]
    if past.empty:
        return None
    return latest_val - past.iloc[-1]


def distance_to_ath(price: float | None, ath: float | None) -> float | None:
    """Percent distance from the all-time high (negative = below ATH)."""
    if price is None or ath is None or ath <= 0:
        return None
    return (price / ath - 1.0) * 100.0


def mc_tvl_ratio(market_cap: float | None, tvl: float | None) -> float | None:
    """Market-cap-to-TVL ratio; None if TVL is missing or non-positive."""
    if market_cap is None or tvl is None or tvl <= 0:
        return None
    return market_cap / tvl


def dilution_ratio(circulating: float | None, max_supply: float | None) -> float | None:
    """Circulating / max supply; None if max supply is missing or non-positive.

    A low ratio flags high future-dilution risk (section 8, SUI case).
    """
    if circulating is None or max_supply is None or max_supply <= 0:
        return None
    return circulating / max_supply


# ---------------------------------------------------------------------------
# DB-backed table builders
# ---------------------------------------------------------------------------


def macro_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Latest value, period change and first-release date per macro series (level 1).

    Columns: key, label, description, series_id, ts, ts_release, value, change_pct.
    ``change_pct`` is the percent change of the latest value vs. the previous
    observation (e.g. month-over-month for CPI). It uses ``abs(prev)`` in the
    denominator so the sign reflects the direction of the move even for series that
    can go negative (e.g. the yield-curve spread). None when there is no prior point
    or the previous value is zero.
    """
    series_cfg: dict[str, Any] = settings.source("fred").get("series", {})
    rows: list[dict[str, Any]] = []
    for key, entry in series_cfg.items():
        code = entry["code"] if isinstance(entry, dict) else entry
        label = entry.get("label", key.upper()) if isinstance(entry, dict) else key.upper()
        description = entry.get("description", "") if isinstance(entry, dict) else ""

        # Last two observations by reference date: [latest, previous].
        last_two = conn.execute(
            "SELECT ts, value, ts_release FROM observations "
            "WHERE source='fred' AND series_id=? AND value IS NOT NULL "
            "ORDER BY ts DESC LIMIT 2",
            (code,),
        ).fetchall()

        latest = last_two[0] if last_two else None
        prev_value = last_two[1][1] if len(last_two) >= 2 else None
        value = latest[1] if latest else None
        change_abs = None
        change_pct = None
        if value is not None and prev_value is not None:
            change_abs = value - prev_value
            if prev_value != 0:
                change_pct = (value - prev_value) / abs(prev_value) * 100.0

        # How the change is displayed: 'percent' (default) or 'absolute' (with a
        # unit suffix), configured per series. NFP uses absolute (jobs added).
        change_display = entry.get("change_display", "percent") if isinstance(entry, dict) else "percent"
        change_unit = entry.get("change_unit", "") if isinstance(entry, dict) else ""
        # How a rise in this indicator maps to crypto: 'inverse' (bearish) / 'direct' (bullish).
        crypto_effect = entry.get("crypto_effect") if isinstance(entry, dict) else None

        rows.append(
            {
                "key": key,
                "label": label,
                "description": description,
                "series_id": code,
                "ts": latest[0] if latest else None,
                "ts_release": latest[2] if latest else None,
                "value": value,
                "change_pct": change_pct,
                "change_abs": change_abs,
                "change_display": change_display,
                "change_unit": change_unit,
                "crypto_effect": crypto_effect,
            }
        )
    return pd.DataFrame(rows)


def portfolio_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Per-asset snapshot: price, 24h/7d/30d change, distance to ATH, dilution (levels 3)."""
    caps = latest_by_source(conn, "coingecko")
    mcaps_by_id = {
        sid.split(":")[0]: val for sid, val in caps.items() if sid.endswith(":market_cap")
    }
    dilution_alert = settings.raw.get("indicators", {}).get("dilution_ratio_alert", 0.6)

    rows: list[dict[str, Any]] = []
    for asset in settings.assets:
        cid = asset["coingecko_id"]
        price = _val(latest_observation(conn, "coingecko", f"{cid}:price"))
        ath = _val(latest_observation(conn, "coingecko", f"{cid}:ath"))
        mcap = _val(latest_observation(conn, "coingecko", f"{cid}:market_cap"))
        vol = _val(latest_observation(conn, "coingecko", f"{cid}:volume_24h"))
        circ = _val(latest_observation(conn, "coingecko", f"{cid}:circulating_supply"))
        maxs = _val(latest_observation(conn, "coingecko", f"{cid}:max_supply"))
        price_hist = series_history(conn, "coingecko", f"{cid}:price")
        dil = dilution_ratio(circ, maxs)
        meta = settings.meta_for(asset["symbol"])
        rows.append(
            {
                "symbol": asset["symbol"],
                "tier": asset["tier"],
                "thesis_category": asset["thesis_category"],
                "logo_url": meta.get("logo_url"),
                "description": meta.get("description", ""),
                "next_unlock": meta.get("next_unlock"),
                "price": price,
                "chg_24h": pct_change_over_days(price_hist, 1),
                "chg_7d": pct_change_over_days(price_hist, 7),
                "chg_30d": pct_change_over_days(price_hist, 30),
                "dist_ath": distance_to_ath(price, ath),
                "market_cap": mcap,
                "volume_24h": vol,
                "dilution_ratio": dil,
                "dilution_risk": (dil is not None and dil < dilution_alert),
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        # Prefer the authoritative whole-market dominance from CoinGecko /global;
        # fall back to the tracked-universe estimate only if it is missing.
        global_dom = latest_observation(conn, "coingecko", "global:btc_dominance")
        df.attrs["btc_dominance"] = (
            global_dom[1] if global_dom is not None else btc_dominance(mcaps_by_id)
        )
        df.attrs["btc_dominance_is_global"] = global_dom is not None
        # Change vs. ~1 month and ~1 year ago, in percentage points (accumulates over
        # time; None until enough dominance history is stored).
        dom_hist = series_history(conn, "coingecko", "global:btc_dominance")
        df.attrs["btc_dominance_chg_30d"] = abs_change_over_days(dom_hist, 30)
        df.attrs["btc_dominance_chg_365d"] = abs_change_over_days(dom_hist, 365)
    return df


def thesis_tvl_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """All tracked assets with TVL where available, grouped by thesis category (level 3).

    Every asset is included so the whole universe reads at a glance; assets without a
    tracked TVL (e.g. BTC, XRP, TAO) show None for the TVL columns. Rows are sorted by
    thesis category (and TVL descending within a category) so same-category tokens sit
    together — exposing concentration by failure mode rather than by ticker (§5).
    """
    rows: list[dict[str, Any]] = []
    for asset in settings.assets:
        dl = asset.get("defillama")
        tvl = tvl_7d = tvl_30d = None
        kind = None
        if dl:
            hist = series_history(conn, "defillama", f"{dl['slug']}:tvl")
            tvl = float(hist.iloc[-1]) if not hist.dropna().empty else None
            tvl_7d = pct_change_over_days(hist, 7)
            tvl_30d = pct_change_over_days(hist, 30)
            kind = dl["kind"]
        mcap = _val(latest_observation(conn, "coingecko", f"{asset['coingecko_id']}:market_cap"))
        meta = settings.meta_for(asset["symbol"])
        rows.append(
            {
                "symbol": asset["symbol"],
                "thesis_category": asset["thesis_category"],
                "kind": kind,
                "tvl": tvl,
                "tvl_chg_7d": tvl_7d,
                "tvl_chg_30d": tvl_30d,
                "mc_tvl": mc_tvl_ratio(mcap, tvl),
                "logo_url": meta.get("logo_url"),
                "description": meta.get("description", ""),
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(
            ["thesis_category", "tvl"], ascending=[True, False], na_position="last"
        ).reset_index(drop=True)
    return df


def dca_status(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Level-4 execution state read from dca_plan: next tranche, deployed, fees.

    Returns a dict with the next pending tranche (or None), USD deployed vs.
    planned, and accumulated fees — material with ~$17 tickets (section 2).
    """
    total_planned = conn.execute("SELECT COALESCE(SUM(amount_usd),0) FROM dca_plan").fetchone()[0]
    deployed = conn.execute(
        "SELECT COALESCE(SUM(amount_usd),0) FROM dca_plan WHERE executed=1"
    ).fetchone()[0]
    fees = conn.execute(
        "SELECT COALESCE(SUM(fees_usd),0) FROM dca_plan WHERE executed=1"
    ).fetchone()[0]
    nxt = conn.execute(
        "SELECT asset, tier, target_date, amount_usd FROM dca_plan "
        "WHERE executed=0 ORDER BY target_date ASC LIMIT 1"
    ).fetchone()
    return {
        "monthly_min_usd": settings.raw.get("dca", {}).get("monthly_contribution_min_usd"),
        "planned_usd": float(total_planned),
        "deployed_usd": float(deployed),
        "fees_usd": float(fees),
        "next_tranche": (
            None
            if nxt is None
            else {"asset": nxt[0], "tier": nxt[1], "target_date": nxt[2], "amount_usd": nxt[3]}
        ),
    }


def _val(observation: tuple[str, float] | None) -> float | None:
    """Extract the value from a (ts, value) tuple, or None."""
    return None if observation is None else observation[1]

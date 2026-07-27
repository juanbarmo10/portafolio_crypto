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
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from core.config import Settings
from db.queries import latest_by_source, latest_observation, series_history, upcoming_events

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


def relative_premium_pct(numerator: float | None, base: float | None) -> float | None:
    """``(numerator / base - 1) * 100`` in percent, or None if inputs are missing/zero.

    The shared primitive for two Q2 signals (IDEAS_MEJORAS A4, A8):
      * perp-vs-spot **basis** — ``relative_premium_pct(perp_close, spot_price)`` — a
        thermometer of leverage/optimism (perp trades above spot = leveraged longs paying
        up; below = pessimism/backwardation).
      * **Coinbase premium** — ``relative_premium_pct(coinbase_px, binance_px)`` — US spot
        demand (positive = ETFs/treasuries bidding on Coinbase).
    Note (§9): for a *perpetual* this is the instantaneous premium, not an annualized
    dated-future basis — perps have no expiry; funding is the carry mechanism.
    """
    if numerator is None or base is None or base == 0:
        return None
    return (numerator / base - 1.0) * 100.0


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


def fed_net_liquidity_series(conn: sqlite3.Connection, settings: Settings) -> pd.Series:
    """Fed **net liquidity** = WALCL − TGA − RRP, in USD billions (level 1, A1).

    The most-followed proxy for how much money is actually available in the system;
    crypto, a long-duration risk asset, tracks it closely (IDEAS_MEJORAS Parte A). The
    three FRED components have mixed units and cadences — WALCL/TGA are weekly and in
    *millions*, RRP is daily and in *billions* — so each is scaled to billions via its
    configured ``unit_scale`` and aligned **as-of** (union of dates, forward-filled)
    before subtracting. Returns an ascending, tz-aware Series in USD billions, or an
    empty Series if any component is missing.
    """
    comps = settings.source("fred").get("liquidity_components", {})
    walcl, tga, rrp = comps.get("walcl"), comps.get("tga"), comps.get("rrp")
    if not (walcl and tga and rrp):
        return pd.Series(dtype="float64")

    def _scaled(entry: dict) -> pd.Series:
        s = series_history(conn, "fred", entry["code"]).dropna()
        return s * float(entry.get("unit_scale", 1.0))

    frames = {"walcl": _scaled(walcl), "tga": _scaled(tga), "rrp": _scaled(rrp)}
    if any(f.empty for f in frames.values()):
        return pd.Series(dtype="float64")
    # As-of align: union of dates, forward-fill each (weekly series carry between
    # updates), then drop rows before all three exist. Subtract in billions.
    df = pd.concat(frames, axis=1, sort=False).sort_index().ffill().dropna()
    if df.empty:
        return pd.Series(dtype="float64")
    return (df["walcl"] - df["tga"] - df["rrp"]).rename("net_liquidity")


def fed_liquidity_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Latest net-liquidity level (USD billions) and 4w/13w % change (level 1, A1)."""
    s = fed_net_liquidity_series(conn, settings)
    if s.empty:
        return {"has_data": False}
    return {
        "has_data": True,
        "latest": float(s.iloc[-1]),
        "latest_ts": s.index[-1].strftime("%Y-%m-%d"),
        "chg_4w_pct": pct_change_over_days(s, 28),
        "chg_13w_pct": pct_change_over_days(s, 91),
        "unit": settings.source("fred").get("net_liquidity", {}).get("unit", "USD billions"),
    }


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


# Status severity ranking (higher = worse) for the invalidation board.
_STATUS_ORDER = {"red": 3, "amber": 2, "green": 1, "na": 0}
_STATUS_BY_SEVERITY = {3: "red", 2: "amber", 1: "green", 0: "na"}


def thesis_invalidation_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Per-asset thesis-invalidation status from the quantitative signals we track (level 3).

    Maps each asset's measurable signals to a green/amber/red light:
      * ETF outflows -> (BTC/ETH) red at ``etf_flow_streak_days`` consecutive out-days,
        amber at >= 2 — their §5 invalidation is "sustained ETF outflows".
      * TVL 7d drop  -> red at ``tvl_drop_pct_7d_alert`` (default 20%), amber at half.
      * Dilution     -> amber when circulating/max < ``dilution_ratio_alert`` (structural).
      * Next unlock  -> red within 7 days, amber within 30 (selling pressure event).
    The status is the worst applicable signal. Assets with no measurable signal (e.g. XRP,
    or tokens whose invalidation is qualitative — HBAR's token demand, BNB's regulatory
    risk) get status ``na`` — the board is honest about what it can and cannot measure.

    Returns columns [symbol, thesis_category, status, reason, invalidation, logo_url],
    sorted worst-first.
    """
    ind = settings.raw.get("indicators", {})
    tvl_red = float(ind.get("tvl_drop_pct_7d_alert", 20.0))
    tvl_amber = tvl_red / 2.0
    dil_alert = float(ind.get("dilution_ratio_alert", 0.6))
    etf_red = int(ind.get("etf_flow_streak_days", 3))
    unlock_pct_alert = float(ind.get("unlock_pct_alert", 5.0))
    today = datetime.now(timezone.utc).date()

    # ETF-flow streaks (BTC/ETH): their §5 invalidation is "sustained ETF outflows".
    # Local import avoids a circular dependency with transform.rally_quality.
    from transform.rally_quality import etf_flow_summary

    etf_streak = {
        r["asset"]: (r["streak_sign"], r["streak_days"])
        for r in etf_flow_summary(conn, settings).to_dict("records")
    }

    rows: list[dict[str, Any]] = []
    for asset in settings.assets:
        symbol = asset["symbol"]
        meta = settings.meta_for(symbol)
        severity = 0
        measurable = False
        reasons: list[str] = []

        etf = etf_streak.get(symbol)
        if etf is not None:
            sign, days = etf
            measurable = True
            if sign < 0 and days >= etf_red:
                severity = max(severity, 3)
                reasons.append(f"salidas ETF {days}d")
            elif sign < 0 and days >= 2:
                severity = max(severity, 2)
                reasons.append(f"salidas ETF {days}d")
            else:
                severity = max(severity, 1)

        dl = asset.get("defillama")
        if dl:
            tvl_7d = pct_change_over_days(series_history(conn, "defillama", f"{dl['slug']}:tvl"), 7)
            if tvl_7d is not None:
                measurable = True
                if tvl_7d <= -tvl_red:
                    severity = max(severity, 3)
                    reasons.append(f"TVL {tvl_7d:.0f}% 7d")
                elif tvl_7d <= -tvl_amber:
                    severity = max(severity, 2)
                    reasons.append(f"TVL {tvl_7d:.0f}% 7d")
                else:
                    severity = max(severity, 1)

        cid = asset["coingecko_id"]
        dil = dilution_ratio(
            _val(latest_observation(conn, "coingecko", f"{cid}:circulating_supply")),
            _val(latest_observation(conn, "coingecko", f"{cid}:max_supply")),
        )
        if dil is not None:
            measurable = True
            if dil < dil_alert:
                severity = max(severity, 2)
                reasons.append(f"dilución alta (circ/máx {dil:.2f})")
            else:
                severity = max(severity, 1)

        raw_unlock = meta.get("next_unlock")
        if raw_unlock:
            try:
                unlock_days = (datetime.strptime(str(raw_unlock), "%Y-%m-%d").date() - today).days
            except ValueError:
                unlock_days = None
            unlock_pct = meta.get("unlock_pct")
            has_pct = isinstance(unlock_pct, (int, float))
            if unlock_days is not None and unlock_days >= 0:
                measurable = True
                # B5: magnitude matters as much as the date (§5: "unlock > 5% del circulante").
                pct_txt = f", {unlock_pct:.0f}% circ." if has_pct else ""
                big = has_pct and unlock_pct >= unlock_pct_alert
                if unlock_days <= 30 and big:  # near AND large -> red
                    severity = max(severity, 3)
                    reasons.append(f"unlock grande en {unlock_days} d{pct_txt}")
                elif unlock_days <= 7:  # imminent -> red regardless of (unknown) size
                    severity = max(severity, 3)
                    reasons.append(f"unlock en {unlock_days} d{pct_txt}")
                elif unlock_days <= 30:  # near, small/unknown -> amber
                    severity = max(severity, 2)
                    reasons.append(f"unlock en {unlock_days} d{pct_txt}")

        status = _STATUS_BY_SEVERITY[severity] if measurable else "na"
        if reasons:
            reason = ", ".join(reasons)
        else:
            reason = "sin señal de alerta" if measurable else "métrica cualitativa (no medible aquí)"
        rows.append(
            {
                "symbol": symbol,
                "thesis_category": asset["thesis_category"],
                "status": status,
                "reason": reason,
                "invalidation": meta.get("description", ""),
                "logo_url": meta.get("logo_url"),
            }
        )

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(
            "status", key=lambda s: s.map(lambda v: -_STATUS_ORDER[v])
        ).reset_index(drop=True)
    return df


def value_accrual_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Protocol activity (TVL) vs. valuation (market cap) — the *concepto rector* (§2).

    The central question for a token is: does price track the activity the protocol
    captures? MC/TVL is the proxy — high = valuation ran ahead of on-chain activity
    (weak value accrual / rich); low = cheap relative to activity. Only assets with a
    tracked TVL **and** a market cap are included (revenue is not yet ingested — a future
    enhancement; TVL is today's activity proxy).

    ``mc_revenue`` (market cap / annualized revenue) is the truer value-capture ratio — a
    crypto P/E. A protocol with big TVL but ~0 revenue to the token (no fee switch, e.g.
    ONDO) has no ``mc_revenue`` (None) — the clearest "no value accrual" signal.

    Returns columns [symbol, thesis_category, tvl, mcap, mc_tvl, revenue_ann, mc_revenue,
    logo_url].
    """
    rows: list[dict[str, Any]] = []
    for asset in settings.assets:
        dl = asset.get("defillama")
        if not dl:
            continue
        hist = series_history(conn, "defillama", f"{dl['slug']}:tvl")
        tvl = float(hist.iloc[-1]) if not hist.dropna().empty else None
        mcap = _val(latest_observation(conn, "coingecko", f"{asset['coingecko_id']}:market_cap"))
        if not tvl or mcap is None:
            continue
        rev_30d = _val(latest_observation(conn, "defillama", f"{dl['slug']}:revenue_30d"))
        revenue_ann = rev_30d * (365.0 / 30.0) if rev_30d is not None else None
        mc_revenue = (mcap / revenue_ann) if (revenue_ann and revenue_ann > 0) else None
        meta = settings.meta_for(asset["symbol"])
        rows.append(
            {
                "symbol": asset["symbol"],
                "thesis_category": asset["thesis_category"],
                "tvl": tvl,
                "mcap": mcap,
                "mc_tvl": mc_tvl_ratio(mcap, tvl),
                "revenue_ann": revenue_ann,
                "mc_revenue": mc_revenue,
                "logo_url": meta.get("logo_url"),
            }
        )
    return pd.DataFrame(rows)


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


# Stablecoins treated as $1 for valuation.
STABLECOINS = {"USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD"}


def _usd_price(
    conn: sqlite3.Connection,
    asset: str,
    id_by_symbol: dict[str, str],
    price_aliases: dict[str, str] | None = None,
) -> float | None:
    """Latest USD price for an asset symbol.

    $1 for stablecoins; else the tracked CoinGecko id; else a ``price_aliases`` id
    (symbol -> coingecko id) for held-but-untracked assets like WBETH. None if unknown.
    """
    if asset in STABLECOINS:
        return 1.0
    coingecko_id = id_by_symbol.get(asset) or (price_aliases or {}).get(asset)
    if not coingecko_id:
        return None
    return _val(latest_observation(conn, "coingecko", f"{coingecko_id}:price"))


def _usd_price_asof(
    conn: sqlite3.Connection,
    asset: str,
    id_by_symbol: dict[str, str],
    price_aliases: dict[str, str] | None,
    ts: str,
) -> float | None:
    """USD price of an asset **as of** ``ts`` (latest price on/before it), else latest.

    Used to value fees at the price when the fee was paid, not today's price — matters
    for non-stablecoin fee currencies (e.g. BNB). $1 for stablecoins.
    """
    if asset in STABLECOINS:
        return 1.0
    coingecko_id = id_by_symbol.get(asset) or (price_aliases or {}).get(asset)
    if not coingecko_id:
        return None
    row = conn.execute(
        "SELECT value FROM observations WHERE source = 'coingecko' AND series_id = ? "
        "AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (f"{coingecko_id}:price", ts),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return _val(latest_observation(conn, "coingecko", f"{coingecko_id}:price"))


def normalize_asset(asset: str, aliases: dict[str, str], priceable: set[str]) -> str:
    """Map a Binance balance code to its priceable underlying asset.

    - Explicit ``aliases`` first (e.g. WBETH -> ETH, staking wrappers).
    - Then strip the ``LD`` prefix of flexible-Earn tokens (LDBTC -> BTC) *only* when the
      remainder is a known priceable asset, so real tickers like LDO are left untouched.
    - Otherwise the code is returned unchanged.
    """
    if asset in aliases:
        return aliases[asset]
    if asset.startswith("LD") and asset[2:] in priceable:
        return asset[2:]
    return asset


def holdings_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Real Binance holdings (+ manual entries) valued in USD (level 4).

    Sums wallet-suffixed ``<ASSET>:balance:<wallet>`` series per normalized asset (Earn,
    staking wrappers, funding), values them (stablecoins $1, tracked ids, or price_aliases
    for WBETH etc.), and appends manual holdings (capital the read-only key cannot read,
    e.g. grid bots). Positions below the dust threshold are dropped. Empty if nothing to
    show. Total and cash are exposed on ``df.attrs``.
    """
    id_by_symbol = {a["symbol"]: a["coingecko_id"] for a in settings.assets}
    cfg = settings.source("binance_account")
    dust = cfg.get("dust_threshold_usd", 1.0)
    aliases = cfg.get("asset_aliases", {}) or {}
    price_aliases = cfg.get("price_aliases", {}) or {}
    priceable = set(id_by_symbol) | STABLECOINS

    # Sum across wallets into one amount per *normalized* asset. Only wallet-suffixed
    # series count, so legacy ``<ASSET>:balance`` rows don't double-count.
    per_asset: dict[str, float] = {}
    for series_id, amount in latest_by_source(conn, "binance").items():
        if ":balance:" not in series_id:
            continue
        asset = normalize_asset(series_id.split(":")[0], aliases, priceable)
        per_asset[asset] = per_asset.get(asset, 0.0) + amount

    rows: list[dict[str, Any]] = []
    for asset, amount in per_asset.items():
        price = _usd_price(conn, asset, id_by_symbol, price_aliases)
        value = None if price is None else amount * price
        if value is not None and value < dust:
            continue
        rows.append(_holding_row(asset, amount, price, value, ""))

    # Manual holdings (grid bots, etc.), shown with a note. Each entry is either a
    # priced position {asset, amount} or a lump sum {label, value_usd}.
    for entry in cfg.get("manual_holdings", []) or []:
        note = entry.get("note", "manual")
        if entry.get("value_usd") is not None:  # lump sum in USD
            label = entry.get("label") or entry.get("asset") or "manual"
            rows.append(_holding_row(label, None, None, float(entry["value_usd"]), note))
            continue
        asset, amount = entry.get("asset"), entry.get("amount")
        if not asset or amount is None:
            continue
        price = _usd_price(conn, asset, id_by_symbol, price_aliases)
        value = None if price is None else float(amount) * price
        rows.append(_holding_row(asset, float(amount), price, value, note))

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    total = df["value_usd"].sum(skipna=True)
    df["weight_pct"] = df["value_usd"] / total * 100 if total else None
    df = df.sort_values("value_usd", ascending=False, na_position="last").reset_index(drop=True)
    df.attrs["total_value_usd"] = float(total) if total else 0.0
    df.attrs["cash_usd"] = float(df.loc[df["is_cash"], "value_usd"].sum(skipna=True))
    return df


def _holding_row(
    asset: str, amount: float, price: float | None, value: float | None, note: str
) -> dict[str, Any]:
    """One holdings row (real Binance or manual)."""
    return {
        "asset": asset,
        "amount": amount,
        "price_usd": price,
        "value_usd": value,
        "is_cash": asset in STABLECOINS,
        "note": note,
    }


def holdings_by_group(
    holdings: pd.DataFrame, settings: Settings, by: str = "thesis_category"
) -> pd.DataFrame:
    """Aggregate priced holdings into groups for the allocation charts (§5).

    Sums real-portfolio USD value grouped by ``by`` — ``"thesis_category"`` (the §5
    concentration view: several RWA positions collapse into one bet), ``"tier"``, or
    ``"asset"``. Cash/stablecoins bucket into ``"Efectivo"``; held-but-untracked assets
    (not in ``settings.assets``, e.g. WBETH) bucket into ``"Otros"``. Rows without a USD
    value are dropped so an unpriced dust position never distorts the weights.

    Args:
        holdings: output of :func:`holdings_table` (needs ``asset``/``value_usd``/``is_cash``).
        settings: to map each symbol to its ``thesis_category``/``tier``.
        by: grouping key — ``"thesis_category"`` | ``"tier"`` | ``"asset"``.

    Returns:
        Columns ``[group, value_usd, weight_pct]``, descending by value. Empty frame
        (same columns) when there is nothing priced to chart.
    """
    empty = pd.DataFrame(columns=["group", "value_usd", "weight_pct"])
    if holdings is None or holdings.empty:
        return empty
    attr_by_symbol = {a["symbol"]: a.get(by) for a in settings.assets}
    # WBETH -> ETH etc.: classify a held-but-untracked symbol under a tracked one.
    aliases = settings.source("binance_account").get("symbol_aliases", {}) or {}

    def _group(row: Mapping[str, Any]) -> str:
        if bool(row.get("is_cash")):
            return "Efectivo"
        sym = aliases.get(row["asset"], row["asset"])
        if by == "asset":
            return str(sym)
        g = attr_by_symbol.get(sym)
        return str(g) if g else "Otros"

    priced = holdings[holdings["value_usd"].notna()].copy()
    if priced.empty:
        return empty
    priced["group"] = priced.apply(_group, axis=1)
    agg = (
        priced.groupby("group", as_index=False)["value_usd"]
        .sum()
        .sort_values("value_usd", ascending=False, na_position="last")
        .reset_index(drop=True)
    )
    total = agg["value_usd"].sum()
    agg["weight_pct"] = agg["value_usd"] / total * 100 if total else 0.0
    return agg


def dca_vs_baseline_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Compare real buy trades against a blind-DCA baseline (§2 behavioral goal).

    For each accumulated asset: your **average entry price** vs. what **blind fixed-dollar
    DCA over the same accumulation window** ``[first_buy, last_buy]`` would have cost — the
    **harmonic** mean of daily prices there (``n / Σ 1/p``), which is the realized average of
    investing a fixed dollar amount each period. ``edge_pp`` = your return minus the
    baseline's; positive = your timing beat blindly averaging in. The baseline needs price
    history covering the window start, so it shows None until backfilled
    (``run_ingest.py --backfill``) — honest, not a fake number.

    Returns per-asset columns [symbol, n_buys, tokens, invested_usd, avg_entry,
    current_price, value_now, actual_ret_pct, baseline_avg, baseline_ret_pct, edge_pp],
    with attrs total_invested_usd / total_value_now_usd across priced positions.
    """
    id_by_symbol = {a["symbol"]: a["coingecko_id"] for a in settings.assets}
    trades = conn.execute(
        "SELECT symbol, side, ts, amount, cost FROM trades ORDER BY ts"
    ).fetchall()

    agg: dict[str, dict[str, Any]] = {}
    for symbol, side, ts, amount, cost in trades:
        base = symbol.split("/")[0]
        if base in STABLECOINS:
            continue
        a = agg.setdefault(
            base, {"tokens": 0.0, "invested": 0.0, "n_buys": 0, "first_ts": ts, "last_ts": ts}
        )
        sign = 1.0 if side == "buy" else -1.0
        a["tokens"] += sign * (amount or 0.0)
        a["invested"] += sign * (cost or 0.0)
        if side == "buy":
            a["n_buys"] += 1
        a["first_ts"] = min(a["first_ts"], ts)
        a["last_ts"] = max(a["last_ts"], ts)

    rows: list[dict[str, Any]] = []
    for base, a in agg.items():
        cid = id_by_symbol.get(base)
        if cid is None or a["tokens"] <= 0 or a["invested"] <= 0:
            continue
        avg_entry = a["invested"] / a["tokens"]
        current = _val(latest_observation(conn, "coingecko", f"{cid}:price"))
        value_now = a["tokens"] * current if current else None
        actual_ret = (current / avg_entry - 1.0) * 100.0 if current else None

        baseline_avg = baseline_ret = edge = None
        hist = series_history(conn, "coingecko", f"{cid}:price").dropna()
        if not hist.empty:
            first, last = pd.Timestamp(a["first_ts"]), pd.Timestamp(a["last_ts"])
            # Blind DCA = fixed dollars per period over the SAME accumulation window
            # [first_buy, last_buy]; its realized cost is the HARMONIC mean of prices
            # (n / Σ 1/p), not the arithmetic mean. Only compute when history covers the
            # window start (else the mean is biased by missing early prices).
            if hist.index.min() <= first:
                window = hist[(hist.index >= first) & (hist.index <= last)]
                window = window[window > 0]
                if len(window) >= 2:
                    baseline_avg = float(len(window) / (1.0 / window).sum())  # harmonic mean
                    if current and baseline_avg:
                        baseline_ret = (current / baseline_avg - 1.0) * 100.0
                        if actual_ret is not None:
                            edge = actual_ret - baseline_ret

        rows.append(
            {
                "symbol": base,
                "n_buys": a["n_buys"],
                "tokens": a["tokens"],
                "invested_usd": a["invested"],
                "avg_entry": avg_entry,
                "current_price": current,
                "value_now": value_now,
                "actual_ret_pct": actual_ret,
                "baseline_avg": baseline_avg,
                "baseline_ret_pct": baseline_ret,
                "edge_pp": edge,
            }
        )

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("invested_usd", ascending=False).reset_index(drop=True)
        df.attrs["total_invested_usd"] = float(df["invested_usd"].sum())
        priced = df[df["value_now"].notna()]
        df.attrs["total_value_now_usd"] = float(priced["value_now"].sum()) if not priced.empty else 0.0
    return df


def _cid_for_symbol(symbol: str, settings: Settings) -> str | None:
    """CoinGecko id for a held symbol (tracked asset or a price alias like WBETH)."""
    for asset in settings.assets:
        if asset["symbol"] == symbol:
            return asset["coingecko_id"]
    aliases = settings.source("binance_account").get("price_aliases", {}) or {}
    return aliases.get(symbol)


def position_cost_basis(
    conn: sqlite3.Connection, settings: Settings, holdings: pd.DataFrame
) -> dict[str, dict[str, Any]]:
    """Per-symbol **average entry price** (USD/token) + source. Trades win over manual.

    Cost is modeled as ``avg_price × current amount`` so it stays consistent with the
    current holding even when trades cover only part of it (BTC: 5 fills but more held
    via earn/Convert) — the trade average is extrapolated to the whole position. **Manual
    cost basis WINS over the trade-derived average**, because the exchange's lifetime
    average is authoritative while ``trades`` only sees a 180-day window. Manual entries
    live in the gitignored ``settings.local.yaml`` under ``binance_account.cost_basis`` as
    ``{avg_price}`` or ``{invested_usd}``.

    Returns ``{symbol: {"avg_price": float, "source": "trades"|"manual"}}``.
    """
    amount_by_symbol = {r["asset"]: r["amount"] for r in holdings.to_dict("records")}
    out: dict[str, dict[str, Any]] = {}

    # Manual cost basis first — it overrides the (windowed) trade average.
    manual = settings.source("binance_account").get("cost_basis", {}) or {}
    for symbol, spec in manual.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("avg_price") is not None:
            out[symbol] = {"avg_price": float(spec["avg_price"]), "source": "manual"}
        elif spec.get("invested_usd") is not None:
            amt = amount_by_symbol.get(symbol)
            if amt:
                out[symbol] = {"avg_price": float(spec["invested_usd"]) / amt, "source": "manual"}

    # Trade-derived average as a fallback for symbols without a manual entry.
    tagg: dict[str, dict[str, float]] = {}
    for symbol, side, amount, cost in conn.execute(
        "SELECT symbol, side, amount, cost FROM trades"
    ).fetchall():
        base = symbol.split("/")[0]
        if base in STABLECOINS:
            continue
        t = tagg.setdefault(base, {"invested": 0.0, "tokens": 0.0})
        sign = 1.0 if side == "buy" else -1.0
        t["invested"] += sign * (cost or 0.0)
        t["tokens"] += sign * (amount or 0.0)
    for base, t in tagg.items():
        if base not in out and t["tokens"] > 0 and t["invested"] > 0:
            out[base] = {"avg_price": t["invested"] / t["tokens"], "source": "trades"}
    return out


def wallet_pnl_table(
    conn: sqlite3.Connection, settings: Settings, holdings: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Per-holding unrealized PnL: current value vs. cost basis (trades or manual). Level 4.

    Cash/stablecoins are excluded (no PnL). Tokens without a cost basis show None PnL
    (honest — add it in settings.local.yaml). attrs carry portfolio totals over positions
    that have a known cost basis.
    """
    if holdings is None:
        holdings = holdings_table(conn, settings)
    cols = ["symbol", "amount", "avg_price", "value_usd", "cost_usd", "pnl_usd", "pnl_pct", "source"]
    if holdings.empty:
        return pd.DataFrame(columns=cols)
    cost = position_cost_basis(conn, settings, holdings)
    rows: list[dict[str, Any]] = []
    for r in holdings.to_dict("records"):
        if r["is_cash"]:
            continue
        amount, price, value = r["amount"], r["price_usd"], r["value_usd"]
        cb = cost.get(r["asset"])
        avg = cb["avg_price"] if cb else None
        cost_usd = (avg * amount) if (avg is not None and amount is not None) else None
        pnl = (value - cost_usd) if (cost_usd is not None and value is not None) else None
        pnl_pct = ((price / avg - 1.0) * 100.0) if (avg and price) else None
        rows.append(
            {
                "symbol": r["asset"],
                "amount": amount,
                "avg_price": avg,
                "value_usd": value,
                "cost_usd": cost_usd,
                "pnl_usd": pnl,
                "pnl_pct": pnl_pct,
                "source": cb["source"] if cb else None,
            }
        )
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df = df.sort_values("value_usd", ascending=False, na_position="last").reset_index(drop=True)
        costed = df[df["cost_usd"].notna()]
        df.attrs["total_value_usd"] = float(df["value_usd"].sum(skipna=True))
        df.attrs["total_cost_usd"] = float(costed["cost_usd"].sum()) if not costed.empty else 0.0
        df.attrs["total_pnl_usd"] = float(costed["pnl_usd"].sum()) if not costed.empty else 0.0
    return df


def _holdings_value_matrix(
    conn: sqlite3.Connection, settings: Settings, holdings: pd.DataFrame
) -> tuple[pd.DataFrame, float]:
    """[date × symbol] USD value of current non-cash holdings at historical prices + cash.

    Basis for the hold-simulation charts: current balances valued along each token's
    stored daily price series (a 'what if I'd held today's portfolio' view — not literal
    past value, since real balance snapshots only start when the account sync began).
    """
    cols: dict[str, pd.Series] = {}
    for r in holdings.to_dict("records"):
        if r["is_cash"] or r["amount"] is None:
            continue
        cid = _cid_for_symbol(r["asset"], settings)
        if cid is None:
            continue
        price_hist = series_history(conn, "coingecko", f"{cid}:price").dropna()
        if not price_hist.empty:
            cols[r["asset"]] = r["amount"] * price_hist
    matrix = pd.concat(cols, axis=1).sort_index().ffill() if cols else pd.DataFrame()
    cash = float(holdings.loc[holdings["is_cash"], "value_usd"].sum(skipna=True))
    return matrix, cash


def wallet_value_history(
    conn: sqlite3.Connection, settings: Settings, holdings: pd.DataFrame | None = None
) -> pd.Series:
    """Daily total value of *current* holdings at historical prices (hold-simulation)."""
    if holdings is None:
        holdings = holdings_table(conn, settings)
    if holdings.empty:
        return pd.Series(dtype="float64")
    matrix, cash = _holdings_value_matrix(conn, settings, holdings)
    if matrix.empty:
        return pd.Series(dtype="float64")
    return (matrix.sum(axis=1, min_count=1) + cash).dropna()


def wallet_pnl_history(
    conn: sqlite3.Connection, settings: Settings, holdings: pd.DataFrame | None = None
) -> pd.Series:
    """Daily unrealized PnL of positions with a known cost basis (value − total cost)."""
    if holdings is None:
        holdings = holdings_table(conn, settings)
    if holdings.empty:
        return pd.Series(dtype="float64")
    cost = position_cost_basis(conn, settings, holdings)
    matrix, _ = _holdings_value_matrix(conn, settings, holdings)
    costed = [c for c in matrix.columns if c in cost]
    if not costed:
        return pd.Series(dtype="float64")
    amount_by_symbol = {r["asset"]: r["amount"] for r in holdings.to_dict("records")}
    total_cost = sum(cost[c]["avg_price"] * amount_by_symbol[c] for c in costed)
    return (matrix[costed].sum(axis=1, min_count=1) - total_cost).dropna()


def execution_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Aggregate real executed trades (level 4): invested, proceeds, fees, count.

    Fees are converted to USD best-effort: stablecoin fees at $1, other fee assets via the
    CoinGecko price **as of the trade date** (not today's); any fee that cannot be priced is
    counted as unconverted.
    """
    trades = conn.execute(
        "SELECT side, cost, fee, fee_currency, ts FROM trades"
    ).fetchall()
    if not trades:
        return {"has_trades": False}

    id_by_symbol = {a["symbol"]: a["coingecko_id"] for a in settings.assets}
    price_aliases = settings.source("binance_account").get("price_aliases", {}) or {}
    invested = proceeds = fees_usd = 0.0
    fees_unconverted = 0
    for side, cost, fee, fee_currency, ts in trades:
        if side == "buy" and cost is not None:
            invested += cost
        elif side == "sell" and cost is not None:
            proceeds += cost
        if fee:
            price = (
                _usd_price_asof(conn, fee_currency, id_by_symbol, price_aliases, ts)
                if fee_currency
                else None
            )
            if price is not None:
                fees_usd += fee * price
            else:
                fees_unconverted += 1

    return {
        "has_trades": True,
        "n_trades": len(trades),
        "invested_usd": invested,
        "proceeds_usd": proceeds,
        "net_invested_usd": invested - proceeds,
        "fees_usd": fees_usd,
        "fees_unconverted": fees_unconverted,
        # Fee drag: commissions as a share of gross capital deployed. Material for the
        # small ~$17 tickets this portfolio uses (sections 1, 4) — every buy pays the
        # taker fee, so many small buys erode more than a few large ones.
        "fees_drag_pct": (fees_usd / invested * 100.0) if invested else None,
        "fees_per_trade_usd": (fees_usd / len(trades)) if trades else None,
    }


def capital_deployed_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Net fiat capital deployed (deposits − withdrawals, USD): the true denominator for
    return on real contributions (sections 1, 2).

    A manual ``binance_account.net_deployed_usd`` (settings.local.yaml) is **authoritative**
    and wins — use it when the auto capture is incomplete (e.g. deposits via channels not
    covered by an API endpoint). Otherwise it sums ``capital_flows`` (fiat orders, fiat
    payments/PSE, P2P). Flows without an FX rate are counted (``unconverted``) but excluded.
    """
    manual = settings.source("binance_account").get("net_deployed_usd")
    rows = conn.execute("SELECT kind, usd_value FROM capital_flows").fetchall()
    if manual is None and not rows:
        return {"has_flows": False}
    deposits = sum(v for k, v in rows if k == "deposit" and v is not None)
    withdrawals = sum(v for k, v in rows if k == "withdraw" and v is not None)
    unconverted = sum(1 for _, v in rows if v is None)
    return {
        "has_flows": True,
        "deposits_usd": deposits,
        "withdrawals_usd": withdrawals,
        "net_deployed_usd": float(manual) if manual is not None else deposits - withdrawals,
        "unconverted": unconverted,
        "is_manual": manual is not None,
    }


def _price_asof_strict(conn: sqlite3.Connection, series_id: str, ts: str) -> float | None:
    """Latest CoinGecko price on/before ``ts``, or None if there is none before it.

    Unlike :func:`_usd_price_asof`, this does NOT fall back to today's price — a missing
    historical price returns None so the caller can flag incomplete coverage instead of
    silently comparing against a wrong (present-day) value.
    """
    row = conn.execute(
        "SELECT value FROM observations WHERE source = 'coingecko' AND series_id = ? "
        "AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (series_id, ts),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def behavioral_scorecard(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """B6: did your trading activity beat buy-and-hold? (§2 anti-over-trading thesis, level 4).

    Builds TWO portfolios from the SAME real cashflows (your ``trades``): the ACTUAL assets you
    bought/sold, and a counterfactual routing every dollar into/out of **BTC** at the trade-date
    price ("hold-BTC"). Compares their current value on the same net-invested base — the direct
    "did my picking/timing beat just holding BTC?" test. Adds friction (fees as % of capital),
    activity (trade count/frequency) and the capital-weighted entry-timing edge vs. blind DCA.

    Honest scope: uses ``trades`` only (both sides), so it measures the capital you actively
    deployed, not holdings acquired via Earn/Convert. Trades whose date has no BTC price — older
    than CoinGecko's free 365-day *daily* window — are excluded from **both** sides (so the bases
    stay aligned) and counted in ``bh_uncovered``; the scorecard then covers the comparable subset.
    """
    trades = conn.execute("SELECT symbol, side, ts, amount, cost FROM trades ORDER BY ts").fetchall()
    if not trades:
        return {"has_data": False}
    id_by_symbol = {a["symbol"]: a["coingecko_id"] for a in settings.assets}

    net_tokens: dict[str, float] = {}
    net_btc = 0.0
    invested = 0.0
    n_buys = n_sells = 0
    bh_uncovered = 0
    ts_list: list[str] = []  # asset (non-stablecoin) trades placed on BOTH sides
    for symbol, side, ts, amount, cost in trades:
        base = symbol.split("/")[0]
        if base in STABLECOINS:
            continue
        btc_px = _price_asof_strict(conn, "bitcoin:price", ts)
        if not (btc_px and cost):
            # No BTC price on this trade's date (older than the free 365-day daily window):
            # exclude it from BOTH sides so the actual and hold-BTC bases stay aligned.
            bh_uncovered += 1
            continue
        ts_list.append(ts)
        sign = 1.0 if side == "buy" else -1.0
        net_tokens[base] = net_tokens.get(base, 0.0) + sign * (amount or 0.0)
        invested += sign * cost
        net_btc += sign * (cost / btc_px)
        if side == "buy":
            n_buys += 1
        else:
            n_sells += 1

    actual_value = 0.0
    for base, tok in net_tokens.items():
        cid = id_by_symbol.get(base)
        px = _val(latest_observation(conn, "coingecko", f"{cid}:price")) if cid else None
        if px is not None:
            actual_value += tok * px
    btc_now = _val(latest_observation(conn, "coingecko", "bitcoin:price"))
    bh_value = net_btc * btc_now if btc_now is not None else None

    # Actual and hold-BTC share the same trade subset and the same net-invested base, so the
    # edge is always comparable; ``bh_uncovered`` reports how many trades were left out.
    actual_ret = (actual_value / invested - 1.0) * 100.0 if invested > 0 else None
    bh_ret = (bh_value / invested - 1.0) * 100.0 if (bh_value is not None and invested > 0) else None
    edge = (actual_ret - bh_ret) if (actual_ret is not None and bh_ret is not None) else None

    try:
        span_days = (
            max((datetime.fromisoformat(max(ts_list)) - datetime.fromisoformat(min(ts_list))).days, 0)
            if ts_list
            else None
        )
    except (ValueError, TypeError):
        span_days = None
    n_trades = n_buys + n_sells
    tpm = (n_trades / (span_days / 30.0)) if span_days and span_days > 0 else None

    exec_sum = execution_summary(conn, settings)
    fees_usd = exec_sum.get("fees_usd") if exec_sum.get("has_trades") else None
    fees_drag = exec_sum.get("fees_drag_pct") if exec_sum.get("has_trades") else None

    dca = dca_vs_baseline_table(conn, settings)
    w_edge = None
    if not dca.empty:
        rows = [r for r in dca.to_dict("records") if r["edge_pp"] is not None and r["invested_usd"] > 0]
        tot = sum(r["invested_usd"] for r in rows)
        if tot > 0:
            w_edge = sum(r["edge_pp"] * r["invested_usd"] for r in rows) / tot

    return {
        "has_data": True,
        "n_trades": n_trades,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "span_days": span_days,
        "trades_per_month": tpm,
        "net_invested_usd": invested,
        "actual_value_usd": actual_value,
        "actual_ret_pct": actual_ret,
        "bh_btc_value_usd": bh_value,
        "bh_btc_ret_pct": bh_ret,
        "edge_vs_hold_btc_pp": edge,
        "bh_uncovered": bh_uncovered,
        "fees_usd": fees_usd,
        "fees_drag_pct": fees_drag,
        "weighted_timing_edge_pp": w_edge,
    }


def earn_rewards_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Passive yield earned via Simple Earn over the window (level 4).

    Reads ``earn:<asset>:rewards`` observations (reward amount in the earning asset), values
    each at the current price, and sums to USD. Rewards in an unpriceable asset are counted
    but not summed.
    """
    series = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT series_id FROM observations "
            "WHERE source = 'binance' AND series_id LIKE 'earn:%:rewards'"
        ).fetchall()
    ]
    if not series:
        return {"has_rewards": False}
    id_by_symbol = {a["symbol"]: a["coingecko_id"] for a in settings.assets}
    price_aliases = settings.source("binance_account").get("price_aliases", {}) or {}
    total_usd = 0.0
    per_asset: list[dict[str, Any]] = []
    for sid in series:
        asset = sid.split(":")[1]
        amount = _val(latest_observation(conn, "binance", sid))
        if not amount:
            continue
        price = _usd_price(conn, asset, id_by_symbol, price_aliases)
        usd = amount * price if price is not None else None
        if usd:
            total_usd += usd
        per_asset.append({"asset": asset, "amount": amount, "usd": usd})
    return {"has_rewards": bool(per_asset), "total_usd": total_usd, "per_asset": per_asset}


def liquidations_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Latest daily liquidation totals (USD) across tracked assets: long vs short (level 2).

    Reads ``<SYMBOL>:liq_long/short:binance`` (from the liquidations daemon). Long > short
    liquidations = longs being flushed (cascade risk / capitulation); the reverse can fuel
    a short squeeze.
    """
    long_usd = short_usd = 0.0
    per_asset: list[dict[str, Any]] = []
    for asset in settings.assets:
        sym = asset["symbol"]
        lo = _val(latest_observation(conn, "derivatives", f"{sym}:liq_long:binance"))
        sh = _val(latest_observation(conn, "derivatives", f"{sym}:liq_short:binance"))
        if lo or sh:
            long_usd += lo or 0.0
            short_usd += sh or 0.0
            per_asset.append({"asset": sym, "long": lo, "short": sh})
    if not per_asset:
        return {"has_liq": False}
    return {"has_liq": True, "long_usd": long_usd, "short_usd": short_usd, "per_asset": per_asset}


# ---------------------------------------------------------------------------
# IDEAS_MEJORAS Parte A — rotation, sentiment, liquidity-proxy & on-chain views
# All read stored observations only (no network). Return dicts the dashboard renders.
# ---------------------------------------------------------------------------


def _asof_frame(series: Mapping[str, pd.Series]) -> pd.DataFrame:
    """Align named series as-of: union of dates, forward-filled, drop until all exist."""
    if any(s.dropna().empty for s in series.values()):
        return pd.DataFrame()
    # sort=False: we sort the index explicitly below; avoids the pandas-4 implicit-sort
    # deprecation on concatenated DatetimeIndex.
    df = pd.concat(dict(series), axis=1, sort=False).sort_index().ffill().dropna()
    return df


def rotation_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """ETH/BTC ratio and TOTAL2/TOTAL3 market caps — rotation / altseason (level 2, A9).

    All from our own stored CoinGecko data: ETH/BTC = classic core→alt rotation barometer;
    TOTAL2 = total mcap excluding BTC; TOTAL3 = excluding BTC **and** ETH. Complements BTC
    dominance for "¿entorno favorable a SOL/LINK?" (§2 nivel 2). Each value carries a 30-day
    change; None until enough history is stored.
    """
    btc_px = series_history(conn, "coingecko", "bitcoin:price")
    eth_px = series_history(conn, "coingecko", "ethereum:price")
    total = series_history(conn, "coingecko", "global:total_market_cap")
    btc_mc = series_history(conn, "coingecko", "bitcoin:market_cap")
    eth_mc = series_history(conn, "coingecko", "ethereum:market_cap")

    ratio = _asof_frame({"n": eth_px, "d": btc_px})
    eth_btc = (ratio["n"] / ratio["d"]).rename("eth_btc") if not ratio.empty else pd.Series(dtype="float64")
    if not eth_btc.empty:
        eth_btc = eth_btc[ratio["d"] != 0]

    t2f = _asof_frame({"total": total, "btc": btc_mc})
    total2 = (t2f["total"] - t2f["btc"]).rename("total2") if not t2f.empty else pd.Series(dtype="float64")
    t3f = _asof_frame({"total": total, "btc": btc_mc, "eth": eth_mc})
    total3 = (
        (t3f["total"] - t3f["btc"] - t3f["eth"]).rename("total3")
        if not t3f.empty
        else pd.Series(dtype="float64")
    )

    def _pack(s: pd.Series) -> dict[str, Any]:
        if s.dropna().empty:
            return {"latest": None, "chg_30d_pct": None}
        return {"latest": float(s.dropna().iloc[-1]), "chg_30d_pct": pct_change_over_days(s, 30)}

    return {
        "has_data": not (eth_btc.dropna().empty and total2.dropna().empty),
        "eth_btc": _pack(eth_btc),
        "total2": _pack(total2),
        "total3": _pack(total3),
    }


def coinbase_premium_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Coinbase-vs-Binance spot premium per asset (US spot demand, level 2, A8).

    Aligns the two daily-close series as-of (last common date) and reports
    ``(coinbase/binance − 1) × 100``. Positive = Coinbase bid richer = US spot demand.
    """
    assets = settings.source("spot_prices").get("assets", ["BTC", "ETH"])
    per: list[dict[str, Any]] = []
    for sym in assets:
        cb = series_history(conn, "spot_prices", f"{sym}:spot_close:coinbase")
        bn = series_history(conn, "spot_prices", f"{sym}:spot_close:binance")
        df = _asof_frame({"cb": cb, "bn": bn})
        if df.empty:
            continue
        prem = relative_premium_pct(float(df["cb"].iloc[-1]), float(df["bn"].iloc[-1]))
        if prem is not None:
            per.append({"asset": sym, "premium_pct": prem, "date": df.index[-1].strftime("%Y-%m-%d")})
    return {"has_data": bool(per), "per_asset": per}


def dvol_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Latest Deribit DVOL (implied-vol index, "crypto VIX") per currency + 30d change (A5)."""
    per: list[dict[str, Any]] = []
    for cur in settings.source("deribit").get("currencies", ["BTC", "ETH"]):
        hist = series_history(conn, "deribit", f"deribit:dvol:{cur.lower()}").dropna()
        if hist.empty:
            continue
        per.append(
            {
                "currency": cur,
                "latest": float(hist.iloc[-1]),
                "chg_30d": abs_change_over_days(hist, 30),
                "date": hist.index[-1].strftime("%Y-%m-%d"),
            }
        )
    return {"has_data": bool(per), "per_currency": per}


# Fear & Greed classification buckets (0-100), matching Alternative.me.
def _fear_greed_label(value: float) -> str:
    if value <= 24:
        return "Miedo extremo"
    if value <= 44:
        return "Miedo"
    if value <= 55:
        return "Neutral"
    if value <= 74:
        return "Codicia"
    return "Codicia extrema"


def fear_greed_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Latest Fear & Greed value (+ classification) and 7-day change (level 1/2, A6)."""
    hist = series_history(conn, "alternative_me", "sentiment:fear_greed").dropna()
    if hist.empty:
        return {"has_data": False}
    value = float(hist.iloc[-1])
    return {
        "has_data": True,
        "value": value,
        "label": _fear_greed_label(value),
        "chg_7d": abs_change_over_days(hist, 7),
        "date": hist.index[-1].strftime("%Y-%m-%d"),
    }


def stablecoins_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Aggregate stablecoin market cap ("dry powder") — latest + 30d change (level 2, A7).

    Growing supply = liquidity entering the ecosystem (fuel); shrinking = capital leaving.
    """
    hist = series_history(conn, "defillama", "stablecoins:total_mcap").dropna()
    if hist.empty:
        return {"has_data": False}
    return {
        "has_data": True,
        "latest": float(hist.iloc[-1]),
        "chg_30d_pct": pct_change_over_days(hist, 30),
        "date": hist.index[-1].strftime("%Y-%m-%d"),
    }


def btc_onchain_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Latest value + 30d % change per configured BTC on-chain chart (level 3, A10).

    Network fundamentals (hash rate, difficulty, tx count, active addresses, miners'
    revenue) as a partial free substitute for paid on-chain metrics (§4). Not MVRV/SOPR.
    """
    charts = settings.source("blockchain_com").get("charts", [])
    per: list[dict[str, Any]] = []
    for chart in charts:
        hist = series_history(conn, "blockchain_com", f"btc:{chart}").dropna()
        if hist.empty:
            continue
        per.append(
            {
                "chart": chart,
                "latest": float(hist.iloc[-1]),
                "chg_30d_pct": pct_change_over_days(hist, 30),
            }
        )
    return {"has_data": bool(per), "per_chart": per}


# ---------------------------------------------------------------------------
# IDEAS_MEJORAS Parte B — B1: regime scoreboard (the "marcador rector")
# ---------------------------------------------------------------------------


def _vote(value: float, low: float, high: float, direction: int = 1) -> int:
    """Vote +/-1/0 for a reading against a dead-zone [low, high]. direction=1: above high
    is risk-on (+1), below low risk-off (-1); direction=-1 inverts (a rise is risk-off)."""
    if value > high:
        return 1 * direction
    if value < low:
        return -1 * direction
    return 0


def regime_scoreboard(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """B1: transparent risk-on / neutral / risk-off regime score (levels 1+2).

    Aggregates a **small, fixed** set of level-1/2 signals — Fed net liquidity, NFCI, HY
    spread, DXY, BTC ETF-flow streak, stablecoin supply, BTC funding z — into one score.
    Each signal votes ``+1`` risk-on / ``0`` neutral / ``-1`` risk-off through a documented
    dead-zone (``indicators.regime`` in config); the score is their **equal-weighted sum**
    and is classified with fixed thresholds. This is the operationalization of §2's hard
    rule ("if L1 and L2 are red, don't buy even if the asset thesis is perfect").

    Deliberately transparent and anti-overfitting (§9): few components, equal fixed
    weights, no weight optimization on history. Components with no data are skipped, so the
    score is over whatever is available (``n`` reports how many voted).

    Returns ``{has_data, score, n, label, components: [{name, reading, vote, rationale}],
    risk_on_at, risk_off_at}`` with ``label`` in {"risk_on","neutral","risk_off"}.
    """
    cfg = settings.raw.get("indicators", {}).get("regime", {})
    nl_dead = float(cfg.get("net_liquidity_dead_pct", 0.5))
    nfci_dead = float(cfg.get("nfci_dead", 0.05))
    hy_dead = float(cfg.get("hy_dead_pp", 0.10))
    dxy_dead = float(cfg.get("dxy_dead_pct", 0.5))
    stab_dead = float(cfg.get("stablecoins_dead_pct", 0.5))
    fund_extreme = float(cfg.get("funding_z_extreme", 2.0))
    etf_min = int(cfg.get("etf_streak_min", 2))
    on_at = int(cfg.get("risk_on_at", 2))
    off_at = int(cfg.get("risk_off_at", -2))

    # Local import avoids a circular dependency with transform.rally_quality.
    from transform.rally_quality import etf_flow_summary, funding_zscore

    components: list[dict[str, Any]] = []

    def add(name: str, reading: str, vote: int, rationale: str) -> None:
        components.append({"name": name, "reading": reading, "vote": vote, "rationale": rationale})

    # 1. Fed net liquidity, 13-week % change (rising = risk-on).
    liq = fed_liquidity_summary(conn, settings)
    if liq.get("has_data") and liq.get("chg_13w_pct") is not None:
        chg = liq["chg_13w_pct"]
        add("Liquidez neta Fed", f"{chg:+.1f}% 13s", _vote(chg, -nl_dead, nl_dead),
            "más liquidez neta = viento de cola risk-on")

    # 2. NFCI level (>0 = restrictive = risk-off; direction inverted).
    nfci = latest_observation(conn, "fred", "NFCI")
    if nfci is not None:
        lvl = nfci[1]
        add("NFCI (condiciones fin.)", f"{lvl:+.2f}", _vote(lvl, -nfci_dead, nfci_dead, direction=-1),
            ">0 = condiciones restrictivas = risk-off")

    # 3. HY spread, 4-week change in pp (widening = risk-off; inverted).
    hy_chg = abs_change_over_days(series_history(conn, "fred", "BAMLH0A0HYM2"), 28)
    if hy_chg is not None:
        add("Spread HY (4s)", f"{hy_chg:+.2f} pp", _vote(hy_chg, -hy_dead, hy_dead, direction=-1),
            "ensanchándose = estrés de crédito = risk-off")

    # 4. DXY, 4-week % change (dollar up = headwind = risk-off; inverted).
    dxy_chg = pct_change_over_days(series_history(conn, "fred", "DTWEXBGS"), 28)
    if dxy_chg is not None:
        add("DXY (4s)", f"{dxy_chg:+.1f}%", _vote(dxy_chg, -dxy_dead, dxy_dead, direction=-1),
            "dólar fuerte = viento en contra = risk-off")

    # 5. BTC ETF-flow streak (sustained inflows = risk-on).
    etf = etf_flow_summary(conn, settings)
    btc_etf = next((r for r in etf.to_dict("records") if r["asset"] == "BTC"), None) if not etf.empty else None
    if btc_etf is not None:
        sign, days = btc_etf["streak_sign"], btc_etf["streak_days"]
        vote = (1 if sign > 0 else -1) if (days >= etf_min and sign != 0) else 0
        word = "entradas" if sign > 0 else "salidas" if sign < 0 else "—"
        add("Flujos ETF BTC", f"{days} d {word}", vote, "entradas sostenidas = demanda spot = risk-on")

    # 6. Stablecoin supply, 30-day % change (growing = liquidity entering = risk-on).
    stab = stablecoins_summary(conn, settings)
    if stab.get("has_data") and stab.get("chg_30d_pct") is not None:
        chg = stab["chg_30d_pct"]
        add("Stablecoins (30 d)", f"{chg:+.1f}%", _vote(chg, -stab_dead, stab_dead),
            "oferta creciente = dry powder entrando = risk-on")

    # 7. BTC funding z: crowded longs (z>+extreme) = froth/cascade risk = risk-off; washed
    # out (z<-extreme) = capitulation/squeeze setup = risk-on. Neutral in between.
    exchanges = settings.source("derivatives").get("exchanges", ["binance", "bybit"])
    window = int(settings.source("derivatives").get("zscore_window_days", 90))
    btc_funding = pd.Series(dtype="float64")
    for ex in exchanges:
        s = series_history(conn, "derivatives", f"BTC:funding:{ex}").dropna()
        if not s.empty:
            btc_funding = s
            break
    if not btc_funding.empty:
        z = funding_zscore(btc_funding, window)
        if z is not None:
            vote = -1 if z > fund_extreme else 1 if z < -fund_extreme else 0
            add("Funding z BTC", f"{z:+.2f}", vote,
                "z>+2 froth (riesgo de cascada) = risk-off; z<-2 lavado = risk-on")

    score = sum(c["vote"] for c in components)
    label = "risk_on" if score >= on_at else "risk_off" if score <= off_at else "neutral"
    return {
        "has_data": bool(components),
        "score": score,
        "n": len(components),
        "label": label,
        "components": components,
        "risk_on_at": on_at,
        "risk_off_at": off_at,
    }


# Tier display labels + §5 order for the drift view (B4).
_TIER_LABELS = {
    "nucleo": "Núcleo",
    "satelite": "Satélite",
    "riesgo_medio": "Riesgo medio",
    "riesgo_alto": "Riesgo alto",
    "observacion": "Observación",
    "sin_tramo": "Sin tramo",
}
_TIER_ORDER = ["nucleo", "satelite", "riesgo_medio", "riesgo_alto", "observacion", "sin_tramo"]


def drift_vs_target(
    conn: sqlite3.Connection, settings: Settings, holdings: pd.DataFrame | None = None
) -> pd.DataFrame:
    """B4: current allocation by tier vs. §5 target weights + rebalance-by-contribution hint.

    Weights are over **invested (non-cash)** holdings — cash is dry powder, not a tramo. Target
    weights (by tier) live in ``portfolio.target_weights`` (config). ``drift_pp`` = current −
    target (percentage points); ``over_band`` flags |drift| > ``portfolio.drift_band_pp``. The
    tier with the most-negative drift among those with a positive target is where the monthly
    contribution should go — rebalance by **adding**, not selling (avoids fees/taxes). Held
    assets outside §5 (e.g. WBETH) fall in ``sin_tramo`` (target 0), shown honestly.

    Returns columns [tier, tier_label, value_usd, current_pct, target_pct, drift_pp, over_band],
    ordered by §5 tramo. attrs: invested_usd, cash_usd, most_underweight (tier key or None).
    """
    if holdings is None:
        holdings = holdings_table(conn, settings)
    cfg = settings.raw.get("portfolio", {})
    targets = cfg.get("target_weights", {}) or {}
    band = float(cfg.get("drift_band_pp", 5))
    cols = ["tier", "tier_label", "value_usd", "current_pct", "target_pct", "drift_pp", "over_band"]
    if holdings.empty:
        return pd.DataFrame(columns=cols)

    tier_by_symbol = {a["symbol"]: a["tier"] for a in settings.assets}
    # Classify held-but-untracked symbols under a tracked one (e.g. WBETH -> ETH -> núcleo).
    aliases = settings.source("binance_account").get("symbol_aliases", {}) or {}
    by_tier: dict[str, float] = {}
    for r in holdings.to_dict("records"):
        val = r["value_usd"]
        # value_usd is None for unpriceable assets (GUN/COP); pandas stores it as NaN in
        # a float column, so `is None` misses it — guard with pd.isna to not poison the sum.
        if r["is_cash"] or val is None or pd.isna(val):
            continue
        asset = aliases.get(r["asset"], r["asset"])
        tier = tier_by_symbol.get(asset, "sin_tramo")
        by_tier[tier] = by_tier.get(tier, 0.0) + float(val)
    invested = sum(by_tier.values())
    cash = float(holdings.loc[holdings["is_cash"], "value_usd"].sum(skipna=True))
    if invested <= 0:
        df = pd.DataFrame(columns=cols)
        df.attrs.update(invested_usd=0.0, cash_usd=cash, most_underweight=None)
        return df

    tiers = [t for t in _TIER_ORDER if t in by_tier or t in targets]
    rows: list[dict[str, Any]] = []
    for tier in tiers:
        cur = by_tier.get(tier, 0.0)
        cur_w = cur / invested * 100.0
        tgt_w = float(targets.get(tier, 0.0))
        drift = cur_w - tgt_w
        rows.append(
            {
                "tier": tier,
                "tier_label": _TIER_LABELS.get(tier, tier),
                "value_usd": cur,
                "current_pct": cur_w,
                "target_pct": tgt_w,
                "drift_pp": drift,
                "over_band": abs(drift) > band,
            }
        )
    df = pd.DataFrame(rows, columns=cols)
    # Most underweight among tiers WITH a positive target (where the contribution goes).
    cand = df[df["target_pct"] > 0]
    most = str(cand.sort_values("drift_pp").iloc[0]["tier"]) if not cand.empty else None
    df.attrs.update(invested_usd=float(invested), cash_usd=cash, most_underweight=most)
    return df


def monthly_contribution_advice(
    conn: sqlite3.Connection,
    settings: Settings,
    within_days: int = 7,
    holdings: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """B7: "should I deploy this month's contribution?" — one monthly, actionable decision.

    **Not a signal generator** — it composes what the panel already computes into your *written
    plan made concrete* (§2 Q4): the **régimen** semáforo (B1), the **events** strip (macro
    releases / FOMC within ``within_days``) and the **drift** most-underweight tramo (B4). It
    recommends **execute now** / **postpone** (until after an imminent high-impact event, or while
    the régimen is risk-off) and **where** (the most under-weighted tramo — rebalance by adding).
    Monthly cadence by design; reinforces discipline, never a buy trigger.

    Returns ``{has_data, minimum_usd, regime_label, regime_score, recommendation
    ('execute'|'postpone'), reasons, postpone_days, blocking_events, target_tier,
    target_tier_label}``.
    """
    minimum = settings.raw.get("dca", {}).get("monthly_contribution_min_usd")
    regime = regime_scoreboard(conn, settings)

    # Imminent high-impact events: macro releases (events table) + FOMC (manual config).
    blocking: list[dict[str, Any]] = [
        {"label": e["label"], "date": e["date"], "days": e["days_until"]}
        for e in upcoming_events(conn, within_days=within_days, categories=("macro",))
    ]
    today = datetime.now(timezone.utc).date()
    for raw in settings.raw.get("macro_calendar", {}).get("fomc_dates", []) or []:
        try:
            days = (datetime.strptime(str(raw), "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if 0 <= days <= within_days:
            blocking.append({"label": "FOMC", "date": str(raw), "days": days})
    blocking.sort(key=lambda b: b["days"])

    drift = drift_vs_target(conn, settings, holdings)
    target_tier = drift.attrs.get("most_underweight") if not drift.empty else None
    target_label = None
    if target_tier and not drift.empty:
        match = drift.loc[drift["tier"] == target_tier, "tier_label"]
        target_label = str(match.iloc[0]) if not match.empty else target_tier

    reasons: list[str] = []
    postpone = False
    if regime.get("has_data") and regime["label"] == "risk_off":
        postpone = True
        reasons.append(f"régimen risk-off (score {regime['score']:+d}): niveles 1-2 en rojo (§2)")
    if blocking:
        postpone = True
        ev = ", ".join(f"{b['label']} {b['date']} (en {b['days']} d)" for b in blocking)
        reasons.append(f"evento(s) de alto impacto inminente(s): {ev}")

    # Event-based postpone has a concrete horizon (wait until after the last one); a régimen-only
    # postpone is open-ended (wait for it to improve) -> None.
    postpone_days = (blocking[-1]["days"] + 1) if blocking else None

    return {
        "has_data": True,
        "minimum_usd": minimum,
        "regime_label": regime.get("label") if regime.get("has_data") else None,
        "regime_score": regime.get("score") if regime.get("has_data") else None,
        "recommendation": "postpone" if postpone else "execute",
        "reasons": reasons,
        "postpone_days": postpone_days,
        "blocking_events": blocking,
        "target_tier": target_tier,
        "target_tier_label": target_label,
    }

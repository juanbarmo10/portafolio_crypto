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


# Status severity ranking (higher = worse) for the invalidation board.
_STATUS_ORDER = {"red": 3, "amber": 2, "green": 1, "na": 0}
_STATUS_BY_SEVERITY = {3: "red", 2: "amber", 1: "green", 0: "na"}


def thesis_invalidation_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Per-asset thesis-invalidation status from the quantitative signals we track (level 3).

    Maps each asset's measurable signals to a green/amber/red light:
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
    today = datetime.now(timezone.utc).date()

    rows: list[dict[str, Any]] = []
    for asset in settings.assets:
        symbol = asset["symbol"]
        meta = settings.meta_for(symbol)
        severity = 0
        measurable = False
        reasons: list[str] = []

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
            if unlock_days is not None and unlock_days >= 0:
                measurable = True
                if unlock_days <= 7:
                    severity = max(severity, 3)
                    reasons.append(f"unlock en {unlock_days} d")
                elif unlock_days <= 30:
                    severity = max(severity, 2)
                    reasons.append(f"unlock en {unlock_days} d")

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

    Returns columns [symbol, thesis_category, tvl, mcap, mc_tvl, logo_url].
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
        meta = settings.meta_for(asset["symbol"])
        rows.append(
            {
                "symbol": asset["symbol"],
                "thesis_category": asset["thesis_category"],
                "tvl": tvl,
                "mcap": mcap,
                "mc_tvl": mc_tvl_ratio(mcap, tvl),
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

    def _group(row: Mapping[str, Any]) -> str:
        if bool(row.get("is_cash")):
            return "Efectivo"
        if by == "asset":
            return str(row["asset"])
        g = attr_by_symbol.get(row["asset"])
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


def wallet_pnl_table(conn: sqlite3.Connection, settings: Settings) -> pd.DataFrame:
    """Per-holding unrealized PnL: current value vs. cost basis (trades or manual). Level 4.

    Cash/stablecoins are excluded (no PnL). Tokens without a cost basis show None PnL
    (honest — add it in settings.local.yaml). attrs carry portfolio totals over positions
    that have a known cost basis.
    """
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


def wallet_value_history(conn: sqlite3.Connection, settings: Settings) -> pd.Series:
    """Daily total value of *current* holdings at historical prices (hold-simulation)."""
    holdings = holdings_table(conn, settings)
    if holdings.empty:
        return pd.Series(dtype="float64")
    matrix, cash = _holdings_value_matrix(conn, settings, holdings)
    if matrix.empty:
        return pd.Series(dtype="float64")
    return (matrix.sum(axis=1, min_count=1) + cash).dropna()


def wallet_pnl_history(conn: sqlite3.Connection, settings: Settings) -> pd.Series:
    """Daily unrealized PnL of positions with a known cost basis (value − total cost)."""
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

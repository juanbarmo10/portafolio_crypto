"""Tests for pure formatting/classification helpers in app.dashboard (CLAUDE.md section 10)."""

from __future__ import annotations

from app.dashboard import (
    _color_by_sign,
    _color_dilution,
    _dilution_tooltip,
    _fmt_category,
    _fmt_change,
    _fmt_date,
    _fmt_pp,
    _fmt_kind,
    _fmt_num,
    _fmt_tier,
    _macro_change_value,
    _macro_delta_str,
    _macro_effect_sentiment,
    _sentiment,
)


def _radar_record(**overrides) -> dict:
    """A minimal portfolio_table record for tooltip tests."""
    base = {
        "symbol": "SUI",
        "tier": "riesgo_alto",
        "dilution_ratio": 0.40,
        "dilution_risk": True,
        "next_unlock": None,
    }
    base.update(overrides)
    return base


def test_fmt_tier_removes_underscore_and_capitalizes() -> None:
    assert _fmt_tier("riesgo_medio") == "Riesgo medio"
    assert _fmt_tier("riesgo_alto") == "Riesgo alto"
    assert _fmt_tier("nucleo") == "Nucleo"


def test_sentiment_classification() -> None:
    assert _sentiment(1.2) == "Alcista"
    assert _sentiment(-0.5) == "Bajista"
    assert _sentiment(0.0) == "Neutral"


def test_macro_effect_sentiment_inverse_and_direct() -> None:
    # inverse: a rise is bearish for crypto (CPI/PCE/NFP/rates/DXY).
    assert _macro_effect_sentiment(0.4, "inverse") == "Bajista para cripto"
    assert _macro_effect_sentiment(-0.4, "inverse") == "Alcista para cripto"
    # direct: a rise is bullish (steepening yield curve).
    assert _macro_effect_sentiment(0.1, "direct") == "Alcista para cripto"
    assert _macro_effect_sentiment(-0.1, "direct") == "Bajista para cripto"
    assert _macro_effect_sentiment(0.0, "inverse") == "Neutral"


def test_macro_delta_str_percent_and_absolute() -> None:
    assert _macro_delta_str({"change_display": "percent", "change_pct": 0.35}) == "▲ +0.35%"
    assert (
        _macro_delta_str({"change_display": "absolute", "change_abs": -17.0, "change_unit": "K"})
        == "▼ -17K"
    )
    assert _macro_delta_str({"change_display": "percent", "change_pct": None}) == "—"


def test_fmt_change_has_arrow_and_sign() -> None:
    assert _fmt_change(2.5) == "▲ +2.50%"
    assert _fmt_change(-1.2) == "▼ -1.20%"
    assert _fmt_change(0.0) == "▬ +0.00%"
    assert _fmt_change(None) == "—"


def test_fmt_pp_percentage_points() -> None:
    assert _fmt_pp(1.2) == "▲ +1.20 pp"
    assert _fmt_pp(-0.8) == "▼ -0.80 pp"
    assert _fmt_pp(None) == "—"


def test_fmt_category_uppercase_no_underscore() -> None:
    assert _fmt_category("defi_lending") == "DEFI LENDING"
    assert _fmt_category("l1_base") == "L1 BASE"
    assert _fmt_category("rwa") == "RWA"


def test_fmt_kind_labels() -> None:
    assert _fmt_kind("protocol") == "Protocolo"
    assert _fmt_kind("chain") == "Cadena"
    assert _fmt_kind(None) == "—"


def test_macro_change_value_picks_right_field() -> None:
    assert _macro_change_value({"change_display": "absolute", "change_abs": 5.0}) == 5.0
    assert _macro_change_value({"change_display": "percent", "change_pct": -1.0}) == -1.0


def test_color_by_sign() -> None:
    assert _color_by_sign("+2.50%") == "color:#16a34a"  # green (bare sign)
    assert _color_by_sign("-1.20%") == "color:#dc2626"  # red
    assert _color_by_sign("▲ +2.50%") == "color:#16a34a"  # green (arrow-prefixed)
    assert _color_by_sign("▼ -1.20%") == "color:#dc2626"  # red
    assert _color_by_sign("▬ +0.00%") == ""  # neutral
    assert _color_by_sign("—") == ""


def test_color_dilution_only_when_flagged() -> None:
    assert "eab308" in _color_dilution("⚠ 0.40")
    assert _color_dilution("0.95") == ""


def test_fmt_num_trims_and_separates() -> None:
    assert _fmt_num(158984.0) == "158,984"
    assert _fmt_num(3.63) == "3.63"


def test_fmt_date_is_date_only() -> None:
    assert _fmt_date("2026-06-01T00:00:00+00:00") == "2026-06-01"
    assert _fmt_date(None) == "—"


def test_dilution_tooltip_content() -> None:
    tip = _dilution_tooltip(_radar_record(dilution_ratio=0.40, dilution_risk=True))
    assert "40.0% del máximo" in tip
    assert "riesgo de dilución alto" in tip
    assert "pendiente" in tip  # no next_unlock set -> Phase 2 pending
    tip2 = _dilution_tooltip(_radar_record(next_unlock="2026-09-01"))
    assert "próximo unlock: 2026-09-01" in tip2
    # Uncapped supply (no max) -> no-cap message, for both None and NaN (DataFrame round-trip).
    assert "sin tope máximo" in _dilution_tooltip(_radar_record(dilution_ratio=None))
    assert "sin tope máximo" in _dilution_tooltip(_radar_record(dilution_ratio=float("nan")))

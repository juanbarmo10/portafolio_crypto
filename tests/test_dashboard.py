"""Tests for pure formatting/classification helpers in app.dashboard (CLAUDE.md section 10)."""

from __future__ import annotations

from app.dashboard import (
    _change_span,
    _delta_cell_html,
    _fmt_date,
    _fmt_num,
    _fmt_tier,
    _portfolio_html,
    _sentiment,
)


def _radar_record(**overrides) -> dict:
    """A minimal portfolio_table record for _portfolio_html tests."""
    base = {
        "symbol": "SUI",
        "tier": "riesgo_alto",
        "logo_url": "",
        "description": "",
        "price": 0.76,
        "chg_24h": None,
        "chg_7d": None,
        "chg_30d": None,
        "dist_ath": -85.0,
        "dilution_ratio": 0.40,
        "dilution_risk": True,
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


def test_change_span_none_is_dash() -> None:
    assert "—" in _change_span(None)


def test_change_span_bullish_bearish() -> None:
    up = _change_span(2.5)
    assert "Alcista" in up and "+2.50%" in up and "#16a34a" in up  # green
    down = _change_span(-1.0)
    assert "Bajista" in down and "-1.00%" in down and "#dc2626" in down  # red


def test_change_span_can_omit_sentiment_tooltip() -> None:
    # Distance-to-ATH keeps the color/arrow but drops the bullish/bearish tooltip.
    cell = _change_span(-47.8, sentiment_tooltip=False)
    assert "title=" not in cell
    assert "#dc2626" in cell and "-47.80%" in cell


def test_macro_delta_cell_has_sentiment_tooltip() -> None:
    # Percent mode and absolute mode both carry the alcista/bajista tooltip.
    pct_cell = _delta_cell_html({"change_display": "percent", "change_pct": 0.35})
    assert "title='Alcista'" in pct_cell and "+0.35%" in pct_cell
    abs_cell = _delta_cell_html(
        {"change_display": "absolute", "change_abs": -17.0, "change_unit": "K"}
    )
    assert "title='Bajista'" in abs_cell and "-17K" in abs_cell


def test_fmt_num_trims_and_separates() -> None:
    assert _fmt_num(158984.0) == "158,984"
    assert _fmt_num(3.63) == "3.63"


def test_fmt_date_is_date_only() -> None:
    assert _fmt_date("2026-06-01T00:00:00+00:00") == "2026-06-01"
    assert _fmt_date(None) == "—"


def test_dilution_icon_is_yellow_and_left_of_value() -> None:
    out = _portfolio_html([_radar_record()])
    assert "#eab308" in out  # yellow warning glyph
    assert out.index("⚠") < out.index("0.40")  # icon sits before the value


def test_no_dilution_icon_when_not_flagged() -> None:
    out = _portfolio_html([_radar_record(dilution_risk=False)])
    assert "⚠" not in out

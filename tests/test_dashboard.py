"""Tests for pure formatting/classification helpers in app.dashboard (CLAUDE.md section 10)."""

from __future__ import annotations

from app.dashboard import (
    _change_span,
    _delta_cell_html,
    _dilution_tooltip,
    _fmt_date,
    _fmt_num,
    _fmt_tier,
    _macro_effect_sentiment,
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
        "market_cap": 1.0e9,
        "volume_24h": 2.0e8,
        "chg_24h": None,
        "chg_7d": None,
        "chg_30d": None,
        "dist_ath": -85.0,
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


def test_macro_delta_cell_generic_sentiment_without_effect() -> None:
    # No crypto_effect configured -> plain directional label.
    pct_cell = _delta_cell_html({"change_display": "percent", "change_pct": 0.35})
    assert "title='Alcista'" in pct_cell and "+0.35%" in pct_cell


def test_macro_effect_sentiment_inverse_and_direct() -> None:
    # inverse: a rise is bearish for crypto (CPI/PCE/NFP/rates/DXY).
    assert _macro_effect_sentiment(0.4, "inverse") == "Bajista para cripto"
    assert _macro_effect_sentiment(-0.4, "inverse") == "Alcista para cripto"
    # direct: a rise is bullish (steepening yield curve).
    assert _macro_effect_sentiment(0.1, "direct") == "Alcista para cripto"
    assert _macro_effect_sentiment(-0.1, "direct") == "Bajista para cripto"
    assert _macro_effect_sentiment(0.0, "inverse") == "Neutral"


def test_macro_delta_cell_uses_crypto_effect() -> None:
    # A rising inflation print is bearish for crypto, though the raw change is positive.
    cell = _delta_cell_html(
        {"change_display": "percent", "change_pct": 0.42, "crypto_effect": "inverse"}
    )
    assert "title='Bajista para cripto'" in cell and "+0.42%" in cell


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


def test_radar_has_market_cap_and_volume_columns() -> None:
    out = _portfolio_html([_radar_record()])
    assert "Cap. mercado" in out
    assert "Vol. 24h" in out


def test_dilution_tooltip_content() -> None:
    tip = _dilution_tooltip(_radar_record(dilution_ratio=0.40, dilution_risk=True))
    assert "40.0% del máximo" in tip
    assert "riesgo de dilución alto" in tip
    assert "pendiente" in tip  # no next_unlock set -> Phase 2 pending
    # With an unlock date supplied it is shown instead.
    tip2 = _dilution_tooltip(_radar_record(next_unlock="2026-09-01"))
    assert "próximo unlock: 2026-09-01" in tip2
    # Uncapped supply (no max) -> no-cap message, for both None and NaN (DataFrame round-trip).
    tip3 = _dilution_tooltip(_radar_record(dilution_ratio=None, dilution_risk=False))
    assert "sin tope máximo" in tip3
    tip4 = _dilution_tooltip(_radar_record(dilution_ratio=float("nan"), dilution_risk=False))
    assert "sin tope máximo" in tip4

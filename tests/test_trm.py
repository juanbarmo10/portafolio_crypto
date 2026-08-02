"""Tests for the TRM (COP/USD) parser (FISCAL.md Paso 2, CLAUDE.md section 10).

Pure parser tests, no network. The TRM feeds the Colombian tax layer, where the cost
basis is frozen at the acquisition-day rate (Art. 269 E.T.), so parsing it correctly
matters fiscally.
"""

from __future__ import annotations

import pytest

from ingest.trm import parse_trm

# Frozen fixture mirroring the datos.gov.co (Socrata) response shape (verified 2026-08-01).
_FIXTURE = [
    {"valor": "3132.42", "unidad": "COP", "vigenciadesde": "2026-07-31T00:00:00.000",
     "vigenciahasta": "2026-07-31T00:00:00.000"},
    {"valor": "3144.14", "unidad": "COP", "vigenciadesde": "2026-08-01T00:00:00.000",
     "vigenciahasta": "2026-08-03T00:00:00.000"},  # weekend carry-over range
]


def test_parse_trm_basic_rows() -> None:
    rows = parse_trm(_FIXTURE)
    assert len(rows) == 2
    r = rows[-1]  # sorted ascending -> last is 2026-08-01
    assert r["source"] == "banrep"
    assert r["series_id"] == "TRM:COP_USD"
    assert r["ts"] == "2026-08-01T00:00:00+00:00"
    assert r["ts_release"] == "2026-08-01T00:00:00+00:00"  # known on its effective date
    assert r["value"] == pytest.approx(3144.14)


def test_parse_trm_dedup_by_day_last_wins() -> None:
    payload = [
        {"valor": "3000.00", "unidad": "COP", "vigenciadesde": "2026-01-02T00:00:00.000"},
        {"valor": "3010.00", "unidad": "COP", "vigenciadesde": "2026-01-02T12:00:00.000"},
    ]
    rows = parse_trm(payload)
    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(3010.0)  # last wins


def test_parse_trm_fails_loudly_on_shape_change() -> None:
    # A silent shape change (missing field) must raise, not yield empty (section 9).
    with pytest.raises((KeyError, ValueError)):
        parse_trm([{"unidad": "COP", "vigenciadesde": "2026-01-02T00:00:00.000"}])  # no 'valor'
    with pytest.raises(ValueError):
        parse_trm({"not": "a list"})  # type: ignore[arg-type]

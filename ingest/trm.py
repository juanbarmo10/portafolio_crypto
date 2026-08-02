"""TRM ingester: Tasa Representativa del Mercado COP/USD (FISCAL.md Paso 2).

The DIAN calculates crypto taxes in Colombian pesos, so every cost basis and disposal
must be converted at the official TRM of the exact day (Art. 269 E.T. — the cost is frozen
at the acquisition-day rate). Source: Banco de la República via datos.gov.co (Socrata),
free and keyless.

The TRM is published per business day and carries over weekends/holidays (``vigenciadesde``
→ ``vigenciahasta`` is a validity range). We store one observation per publication date and
rely on **as-of** lookups downstream (the same pattern as ``_usd_price_asof``), so any trade
date resolves to the last TRM at or before it — no row explosion.

series_id ``TRM:COP_USD`` (COP per 1 USD), source ``banrep``. This is **public** data, so it
may sync to the shared/cloud DB (unlike the personal tax tables it feeds).

fetch() -> DataFrame[source, series_id, ts, ts_release, value]
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import requests

from core.config import Settings
from core.logging_setup import get_logger
from ingest.base import Ingester, retry

log = get_logger(__name__)


def parse_trm(
    payload: list[dict], series_id: str = "TRM:COP_USD", source: str = "banrep"
) -> list[dict[str, Any]]:
    """Socrata TRM rows -> long observation rows (pure, testable).

    Each row is ``{"valor": "3144.14", "unidad": "COP", "vigenciadesde": "2026-08-01T00:00:00.000",
    "vigenciahasta": ...}``. ``ts`` = the effective date (``vigenciadesde``) truncated to the UTC
    day; ``value`` = COP per USD. ``ts_release`` = ``ts`` (the rate is known on its effective date).
    Dedup by day (last wins). **Fails loudly** if the response is not a list or a row lacks the
    expected fields (§9 — a silent shape change must not pass as empty).
    """
    if not isinstance(payload, list):
        raise ValueError(f"TRM: expected a JSON list, got {type(payload).__name__} (shape changed?).")
    by_day: dict[str, float] = {}
    for item in payload:
        raw_date = item["vigenciadesde"]  # KeyError = loud failure on shape change
        value = float(item["valor"])
        day = str(raw_date)[:10]  # 'YYYY-MM-DD'
        by_day[day] = value
    return [
        {
            "source": source,
            "series_id": series_id,
            "ts": f"{day}T00:00:00+00:00",
            "ts_release": f"{day}T00:00:00+00:00",
            "value": value,
        }
        for day, value in sorted(by_day.items())
    ]


class TrmIngester(Ingester):
    """Fetch the COP/USD TRM daily history (public, keyless). FISCAL.md Paso 2."""

    source = "banrep"

    def __init__(self, settings: Settings) -> None:
        cfg = settings.raw.get("fiscal", {}).get("trm", {})
        self._base_url = cfg.get("base_url", "https://www.datos.gov.co/resource/32sa-8pi3.json")
        self._timeout = cfg.get("request_timeout_s", 25)
        self._since = cfg.get("history_since", "2024-01-01")
        self._series_id = cfg.get("series_id", "TRM:COP_USD")
        self.source = cfg.get("source", "banrep")

    def _fetch(self) -> list[dict]:
        resp = requests.get(
            self._base_url,
            params={
                "$where": f"vigenciadesde >= '{self._since}T00:00:00'",
                "$limit": 100000,
                "$order": "vigenciadesde ASC",
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> pd.DataFrame:
        """Return the TRM daily history as a long observations DataFrame."""
        try:
            payload = retry(self._fetch, exceptions=(requests.RequestException,))
        except requests.RequestException:
            log.warning("TRM: request failed; skipping.")
            return self.validate(
                pd.DataFrame(columns=["source", "series_id", "ts", "ts_release", "value"])
            )
        rows = parse_trm(payload, series_id=self._series_id, source=self.source)
        log.info("TRM (COP/USD): %d daily points since %s.", len(rows), self._since)
        return self.validate(pd.DataFrame(rows, columns=["source", "series_id", "ts", "ts_release", "value"]))

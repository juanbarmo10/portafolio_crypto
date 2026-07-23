"""ETF-flows ingester: Farside spot-ETF daily flows (CLAUDE.md sections 4, 8, 9).

Scrapes the daily BTC/ETH spot-ETF flow tables from farside.co.uk and stores the
per-issuer flows plus the daily total. Covers checklist level 2 (§2): sustained ETF
inflows vs. an isolated spike, and the streak of positive/negative days.

Fragile by design (§9): the parser is a pure function tested against a frozen HTML
fixture, and it **fails loudly** (raises) if the table structure changes, rather than
silently emitting nothing. Robots.txt only disallows Twitterbot; we send an
identifiable User-Agent and the daily ingest cadence keeps us to ~1 request/day.

Values are in **USD millions** (as Farside reports them).

fetch() -> DataFrame[source, series_id, ts, ts_release, value]
    series_id = "etf:<asset>:<issuer>" | "etf:<asset>:total"
    e.g. "etf:btc:IBIT", "etf:btc:total", "etf:eth:total".
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

import pandas as pd
import requests

from core.config import Settings
from core.logging_setup import get_logger
from db.loader import OBSERVATION_COLUMNS
from ingest.base import Ingester, retry

log = get_logger(__name__)

# Cells Farside uses for "no data".
_EMPTY_CELLS = {"-", "", "nan", "none", "—"}


def _parse_flow(cell: object) -> float | None:
    """Parse a Farside flow cell to a float (millions), or None if it is not data.

    Handles thousands separators and parenthesised negatives, e.g. '1,234.5' ->
    1234.5, '(63.3)' -> -63.3, '-' -> None.
    """
    s = str(cell).strip()
    if s.lower() in _EMPTY_CELLS:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def _parse_date(cell: object) -> datetime | None:
    """Parse a Farside date cell like '06 Jul 2026', or None (summary rows, etc.)."""
    try:
        return datetime.strptime(str(cell).strip(), "%d %b %Y")
    except (ValueError, TypeError):
        return None


def _issuer_ticker(col: object) -> str | None:
    """Return the issuer ticker from a column header, or None for date/total/blank."""
    if isinstance(col, tuple) and len(col) >= 2:
        level1 = str(col[1])
        if not level1.startswith("Unnamed"):
            return level1
    return None


def _is_total_col(col: object) -> bool:
    """True if a column header marks the daily Total column."""
    parts = col if isinstance(col, tuple) else (col,)
    return any(str(p).strip() == "Total" for p in parts)


def _select_flow_table(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Pick the daily-flow table: the one whose first column has parseable dates.

    Raises:
        ValueError: If no table looks like the flow table (structure changed, §9).
    """
    for table in tables:
        if table.shape[1] < 3:
            continue
        first_col = table.iloc[:, 0]
        if any(_parse_date(v) is not None for v in first_col):
            return table
    raise ValueError(
        "Farside: no daily-flow table found (no column of parseable dates). "
        "The page structure likely changed."
    )


def parse_farside(html: str, asset: str) -> pd.DataFrame:
    """Parse a Farside ETF-flow page into the long observations contract.

    Pure function (no network) so it is unit-testable against a frozen fixture.

    Args:
        html: Raw HTML of a Farside ``/<asset>/`` page.
        asset: Asset slug used in series ids, e.g. "btc" or "eth".

    Returns:
        DataFrame ``[source, series_id, ts, ts_release, value]`` with one row per
        (issuer, date) plus a per-date total. Values are USD millions.

    Raises:
        ValueError: If the flow table or its total column cannot be located, or if
            no data rows are produced (fail loudly on structure change, §9).
    """
    tables = pd.read_html(io.StringIO(html))
    table = _select_flow_table(tables)

    columns = list(table.columns)
    total_indices = [j for j, c in enumerate(columns) if _is_total_col(c)]
    if not total_indices:
        raise ValueError("Farside: could not locate the 'Total' column (structure changed).")
    total_idx = total_indices[-1]
    issuer_cols = [(j, _issuer_ticker(c)) for j, c in enumerate(columns) if _issuer_ticker(c)]

    rows: list[dict[str, Any]] = []
    for _, record in table.iterrows():
        parsed_date = _parse_date(record.iloc[0])
        if parsed_date is None:
            continue  # summary rows (Total/Average/Maximum/Minimum) and noise
        ts = parsed_date.strftime("%Y-%m-%dT00:00:00+00:00")

        total = _parse_flow(record.iloc[total_idx])
        if total is not None:
            rows.append(_row(asset, "total", ts, total))
        for j, ticker in issuer_cols:
            value = _parse_flow(record.iloc[j])
            if value is not None:
                rows.append(_row(asset, ticker, ts, value))

    if not rows:
        raise ValueError(f"Farside {asset}: parsed zero data rows (structure changed?).")
    return pd.DataFrame(rows, columns=OBSERVATION_COLUMNS)


def _row(asset: str, issuer: str, ts: str, value: float) -> dict[str, Any]:
    """Build one observation row for the ETF-flows source."""
    return {
        "source": "farside",
        "series_id": f"etf:{asset}:{issuer}",
        "ts": ts,
        "ts_release": None,
        "value": value,
    }


class EtfFlowsIngester(Ingester):
    """Fetch and parse Farside daily spot-ETF flows for the configured assets."""

    source = "farside"

    def __init__(self, settings: Settings) -> None:
        """Read the etf_flows config block."""
        cfg = settings.source("etf_flows")
        self._base_url: str = cfg.get("base_url", "https://farside.co.uk")
        self._assets: list[str] = cfg.get("assets", ["btc", "eth"])
        self._timeout: int = int(cfg.get("request_timeout_s", 25))
        self._user_agent: str = cfg.get("user_agent", "cryptodash/0.1")

    def _fetch_html(self, asset: str) -> str:
        """GET the Farside page for an asset with an identifiable User-Agent."""
        url = f"{self._base_url}/{asset}/"
        resp = requests.get(url, headers={"User-Agent": self._user_agent}, timeout=self._timeout)
        resp.raise_for_status()
        return resp.text

    def fetch(self) -> pd.DataFrame:
        """Return daily ETF flows for all configured assets as a long DataFrame."""
        frames: list[pd.DataFrame] = []
        for asset in self._assets:
            html = retry(
                lambda a=asset: self._fetch_html(a), exceptions=(requests.RequestException,)
            )
            frame = parse_farside(html, asset)
            log.info("Farside: %s -> %d observations.", asset, len(frame))
            frames.append(frame)

        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=OBSERVATION_COLUMNS)
        return self.validate(df)

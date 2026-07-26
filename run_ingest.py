"""Pipeline entry point (CLAUDE.md section 8, Phase 0 acceptance criterion).

Usage:
    python run_ingest.py --dry-run   # create/verify the DB and config, ingest nothing
    python run_ingest.py             # run registered ingesters (added in Phase 1+)

Inputs (read):
    - config/settings.yaml (+ config/.env) via core.config.
    - db/schema.sql via db.loader.

Outputs (write):
    - The SQLite database (schema ensured; observations upserted in non-dry-run).
    - Structured logs to stderr.

Phase 0 acceptance criterion:
    ``python run_ingest.py --dry-run`` creates the DB and does not fail.
"""

from __future__ import annotations

import argparse
import sys

from core.config import Settings, load_settings
from core.logging_setup import configure_logging, get_logger
from db.loader import (
    init_db,
    upsert_capital_flows,
    upsert_events,
    upsert_observations,
    upsert_trades,
)
from ingest.base import Ingester
from ingest.binance_account import BinanceAccountIngester
from ingest.coingecko import CoinGeckoIngester
from ingest.defillama import DefiLlamaIngester
from ingest.derivatives import DerivativesIngester
from ingest.etf_flows import EtfFlowsIngester
from ingest.fred import FredIngester
from ingest.fred_releases import FredReleasesIngester

log = get_logger(__name__)


def build_ingesters(
    settings: Settings, public: bool = False, full_history: bool = False
) -> list[Ingester]:
    """Instantiate the registered ingesters, skipping any that cannot run.

    FRED needs FRED_API_KEY; if it is absent that ingester is skipped with a
    warning rather than aborting the whole run (CoinGecko and DefiLlama need no key).

    Args:
        public: When True the private **Binance account** sync is skipped, so a
            shared/cloud database (e.g. Neon feeding a public dashboard) never receives
            your real holdings/trades. Public market data only.
    """
    ingesters: list[Ingester] = [
        CoinGeckoIngester(settings),
        DefiLlamaIngester(settings),
        DerivativesIngester(settings),
        EtfFlowsIngester(settings),
    ]
    try:
        ingesters.append(FredIngester(settings))
    except RuntimeError as exc:
        log.warning("Skipping FRED ingester: %s", exc)
    try:
        ingesters.append(FredReleasesIngester(settings))
    except RuntimeError as exc:
        log.warning("Skipping FRED releases ingester: %s", exc)
    if public:
        log.info("Public ingest: skipping the Binance account sync (no private holdings).")
    else:
        # Read-only Binance account sync (holdings + trades); needs API keys.
        # full_history: deep Convert/Earn/capital-flows reconstruction (--backfill only).
        try:
            ingesters.append(BinanceAccountIngester(settings, full_history=full_history))
        except RuntimeError as exc:
            log.warning("Skipping Binance account ingester: %s", exc)
    return ingesters


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments. Returns the parsed namespace."""
    parser = argparse.ArgumentParser(description="cryptodash ingestion pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create/verify the DB and config, but run no ingesters.",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Public market data only: skip the private Binance account sync "
        "(use when writing to a shared/cloud DB feeding a public dashboard).",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="One-time historical backfill (CoinGecko daily prices) instead of the "
        "daily snapshot; gives change/return/baseline calcs real history.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the pipeline.

    In dry-run mode this validates configuration and ensures the schema exists,
    which is all Phase 0 requires. Concrete ingesters are registered here in
    later phases.

    Returns:
        Process exit code (0 on success).
    """
    args = parse_args(argv)

    settings = load_settings()
    configure_logging(settings.log_level)

    log.info(
        "Starting run_ingest (dry_run=%s, db=%s, assets=%d).",
        args.dry_run,
        settings.db_path,
        len(settings.assets),
    )

    # Ensure the schema exists regardless of mode; this is the Phase 0 deliverable.
    conn = init_db(settings.db_path)
    try:
        if args.dry_run:
            log.info("Dry run: schema ensured, no ingesters executed. Done.")
            return 0

        if args.backfill:
            filled = 0
            # full_history=True -> deep Convert/Earn/capital-flows reconstruction (slow, once).
            for ingester in build_ingesters(settings, public=args.public, full_history=True):
                if hasattr(ingester, "fetch_history"):  # CoinGecko daily price history
                    filled += upsert_observations(conn, ingester.fetch_history())
                if hasattr(ingester, "fetch_trades"):  # deep Convert history -> cost basis
                    upsert_trades(conn, ingester.fetch_trades())
                if hasattr(ingester, "fetch_capital_flows"):
                    upsert_capital_flows(conn, ingester.fetch_capital_flows())
            log.info("Backfill complete: %d historical observations upserted.", filled)
            return 0

        total = 0
        failures = 0
        for ingester in build_ingesters(settings, public=args.public):
            name = type(ingester).__name__
            try:
                df = ingester.fetch()
                total += upsert_observations(conn, df)
                # Some ingesters also produce trades (Binance account, level 4).
                if hasattr(ingester, "fetch_trades"):
                    upsert_trades(conn, ingester.fetch_trades())
                # ...or calendar events (FRED release dates -> events strip / alerts).
                if hasattr(ingester, "fetch_events"):
                    upsert_events(conn, ingester.fetch_events())
                # ...or capital flows (Binance fiat deposits/withdrawals, level 4).
                if hasattr(ingester, "fetch_capital_flows"):
                    upsert_capital_flows(conn, ingester.fetch_capital_flows())
            except Exception:  # noqa: BLE001 — log, keep going, fail loudly at the end
                failures += 1
                log.exception("Ingester %s failed.", name)

        log.info("Ingest complete: %d observations upserted, %d ingester(s) failed.",
                 total, failures)
        # Non-zero exit if any ingester failed, so cron/CI surfaces it (section 9).
        return 1 if failures else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

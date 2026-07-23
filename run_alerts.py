"""Alerts entry point (CLAUDE.md section 8, phase 3).

Evaluates the declarative alert rules against the current DB and sends any new alert to
Telegram (or logs it in dry-run when Telegram is not configured). Deduped via alerts_log.
Intended to run after ``run_ingest.py`` (cron/CI).

Usage:
    python run_alerts.py            # evaluate + send new alerts
    python run_alerts.py --dry-run  # evaluate + log, never send (still records dedup)

Reads: the SQLite DB (Settings.db_path) and config.
Writes: alerts_log rows; Telegram messages (unless dry-run/unconfigured).
"""

from __future__ import annotations

import argparse
import sys

from alerts.rules import dispatch_alerts
from alerts.telegram import TelegramSender
from core.config import load_settings
from core.logging_setup import configure_logging, get_logger
from db.loader import init_db

log = get_logger(__name__)


class _NullSender:
    """Sender that never sends (used by --dry-run); logs the message."""

    def send(self, message: str) -> bool:
        log.info("Alert (dry-run):\n%s", message)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cryptodash alert evaluation")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate and log, never send.")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.log_level)
    sender = _NullSender() if args.dry_run else TelegramSender(settings)
    if not args.dry_run and not sender.enabled:
        log.warning("Telegram not configured (TELEGRAM_TOKEN/CHAT_ID); alerts will be logged only.")

    conn = init_db(settings.db_path)
    try:
        fired, sent = dispatch_alerts(conn, settings, sender)
        log.info("Done: %d fired, %d sent.", fired, sent)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

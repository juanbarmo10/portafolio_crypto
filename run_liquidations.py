"""Liquidations collector daemon (CLAUDE.md sections 4, 8; level 2).

Long-running process: subscribes to Binance's ``!forceOrder@arr`` WebSocket, buckets USD
liquidation volume per (asset, UTC day, side) via :class:`ingest.liquidations.LiquidationBuffer`,
and flushes running daily totals to the DB every ``liquidations_flush_s`` seconds. Public
data (Q2). Auto-reconnects on disconnect. Run under systemd (see deploy/).

Usage:
    python run_liquidations.py            # runs until stopped
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pandas as pd

try:
    import websockets
except ImportError as exc:  # pragma: no cover - needs the .[markets] extra
    raise SystemExit("websockets not installed. Install: pip install -e '.[markets]'") from exc

from core.config import load_settings
from core.logging_setup import configure_logging, get_logger
from db.loader import init_db, upsert_observations
from ingest.liquidations import WS_URL, LiquidationBuffer

log = get_logger(__name__)


async def _run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    tracked = {a["symbol"] for a in settings.assets}
    quote = settings.source("derivatives").get("quote", "USDT")
    flush_every = float(settings.source("derivatives").get("liquidations_flush_s", 60))
    buffer = LiquidationBuffer(tracked)
    conn = init_db(settings.db_path)
    last_flush = time.monotonic()
    log.info("Liquidations daemon: %d tracked assets, flush every %.0fs.", len(tracked), flush_every)

    async for ws in websockets.connect(WS_URL, ping_interval=20, ping_timeout=20):
        try:
            async for message in ws:
                data = json.loads(message)
                for event in data if isinstance(data, list) else [data]:
                    buffer.add_event(event, quote)
                if time.monotonic() - last_flush >= flush_every:
                    rows = buffer.rows()
                    if rows:
                        upsert_observations(conn, pd.DataFrame(rows))
                    buffer.prune_before(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                    last_flush = time.monotonic()
        except websockets.ConnectionClosed:
            log.warning("Liquidations WS closed; reconnecting.")
            continue


def main() -> int:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Liquidations daemon stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

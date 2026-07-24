#!/usr/bin/env bash
# Push PUBLIC market data to the cloud DB (Neon) for the deployed dashboard.
#
# Runs `run_ingest.py --public` (NO Binance account sync — holdings never leave your
# machine) against the database in DATABASE_URL. Kept separate from run_daily.sh so the
# LOCAL run stays on SQLite with the full (private) data. Logs to logs/neon_sync.log.
#
# DATABASE_URL is provided by the systemd service's EnvironmentFile
# (~/.config/cryptodash/neon.env); if it is empty the sync is skipped, not run against SQLite.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python"
mkdir -p "$DIR/logs"
LOG="$DIR/logs/neon_sync.log"

{
    echo "==================== $(date -Is) neon sync ===================="
    if [ -z "${DATABASE_URL:-}" ]; then
        echo "DATABASE_URL not set — skipping. Put it in ~/.config/cryptodash/neon.env"
        exit 0
    fi
    "$PY" "$DIR/run_ingest.py" --public
    echo "result: exit=$?"
} >> "$LOG" 2>&1

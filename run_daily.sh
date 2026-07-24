#!/usr/bin/env bash
# Daily orchestration for cron / systemd timer: ingest -> alerts (+ weekly validation).
#
# Uses the project venv directly (no activation needed) and resolves the repo from this
# script's location, so it works from any working directory. Appends timestamped output
# to logs/daily.log. Alerts run even if ingest exits non-zero (a single failed source
# must not suppress the rest).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python"
mkdir -p "$DIR/logs"
LOG="$DIR/logs/daily.log"

{
    echo "==================== $(date -Is) daily run ===================="
    echo "--- ingest ---"
    "$PY" "$DIR/run_ingest.py"; ingest_exit=$?
    echo "--- alerts ---"
    "$PY" "$DIR/run_alerts.py"; alerts_exit=$?
    if [ "$(date +%u)" = "7" ]; then          # Sunday: weekly signal validation
        echo "--- validation (weekly) ---"
        "$PY" "$DIR/run_validation.py" || true
    fi
    echo "result: ingest_exit=$ingest_exit alerts_exit=$alerts_exit"
} >> "$LOG" 2>&1

#!/usr/bin/env bash
# Launch the cryptodash Streamlit dashboard with a single command.
#
# Resolves the repo root from this script's location and uses the project venv
# directly, so it works from any directory and without activating the venv first.
#
# Usage:
#   ./run_dashboard.sh                      # default port 8501
#   ./run_dashboard.sh --server.port 8502   # extra args pass through to streamlit
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STREAMLIT="$DIR/.venv/bin/streamlit"

if [[ ! -x "$STREAMLIT" ]]; then
    echo "Streamlit not found in the venv ($STREAMLIT)." >&2
    echo "Create the venv and install the app extra first:" >&2
    echo "    python3 -m venv .venv && .venv/bin/pip install -e \"$DIR[app]\"" >&2
    exit 1
fi

exec "$STREAMLIT" run "$DIR/app/dashboard.py" "$@"

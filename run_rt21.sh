#!/usr/bin/env bash
#
# Launcher for the RT-21 Rotator Controller, web edition (macOS / Linux).
# Standard library only — no virtual environment, nothing to install.
#
# Usage:  ./run_rt21.sh [--demo] [--host 192.168.7.203] [--port 6555]
#                       [--listen 0.0.0.0] [--no-browser] [-v]

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SCRIPT="$APP_DIR/rt21_web.py"

if [ ! -f "$APP_SCRIPT" ]; then
    echo "Error: $APP_SCRIPT not found." >&2
    exit 1
fi

PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "Error: no python3 found on PATH." >&2
    exit 1
fi

echo "Starting RT-21 Controller with $("$PYTHON" --version)"
exec "$PYTHON" "$APP_SCRIPT" "$@"

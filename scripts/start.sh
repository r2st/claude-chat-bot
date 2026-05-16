#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "ERROR: .env not found. Run ./scripts/install.sh first."
    exit 1
fi

set -a
source "$PROJECT_DIR/.env"
set +a

if [ -d "$PROJECT_DIR/venv" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python"
else
    PYTHON="python3"
fi

echo "Starting bot (mode: ${CLAUDE_MODE:-cli})..."
exec "$PYTHON" "$PROJECT_DIR/bot.py"

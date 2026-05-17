#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> Installing telechat"

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required. Install it first."
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "    Python $PYTHON_VERSION found"

# Create venv
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "==> Creating virtual environment..."
    python3 -m venv "$PROJECT_DIR/venv"
fi

echo "==> Installing dependencies..."
"$PROJECT_DIR/venv/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"

# Create .env if missing
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "==> Created .env from template — edit it with your tokens"
else
    echo "    .env already exists, skipping"
fi

echo ""
echo "Done! Next steps:"
echo "  1. Edit .env and set BOT_MODE: telegram | whatsapp | both"
echo ""
echo "  Telegram setup:"
echo "    - Set TELEGRAM_BOT_TOKEN (get from @BotFather)"
echo ""
echo "  WhatsApp setup (Green API — free, no Meta account needed):"
echo "    - Sign up at https://console.green-api.com"
echo "    - Create an instance → Scan QR with your WhatsApp phone"
echo "    - Set GREEN_API_INSTANCE_ID and GREEN_API_TOKEN in .env"
echo ""
echo "  2. Set CLAUDE_MODE to 'cli' or 'api'"
echo "     - cli: requires 'claude' CLI installed and authenticated"
echo "     - api: requires ANTHROPIC_API_KEY in .env"
echo "  3. Run: ./scripts/start.sh"
echo "  4. (Optional) Install as service: ./scripts/service.sh install"

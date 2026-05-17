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

BOT_MODE="${BOT_MODE:-telegram}"

# ── WhatsApp: prompt for allowed numbers if not set ──
if [[ "$BOT_MODE" == *whatsapp* || "$BOT_MODE" == "both" || "$BOT_MODE" == "all" ]]; then
    if [ -z "${WHATSAPP_ALLOWED_NUMBERS:-}" ]; then
        echo ""
        echo "⚠  WhatsApp access control not configured."
        echo "   Without WHATSAPP_ALLOWED_NUMBERS, anyone can message your bot."
        echo ""
        echo "   Enter your WhatsApp number (without +, e.g. 919876543210)"
        echo "   Tip: send !id to the bot to discover your number"
        echo ""
        read -r -p "   Number(s) to allow (comma-sep, enter=allow all): " WA_NUMS
        if [ -n "$WA_NUMS" ]; then
            # Clean up: remove spaces, +, dashes, parens
            WA_NUMS=$(echo "$WA_NUMS" | tr -d ' +-()')
            # Update .env file
            if grep -q "^WHATSAPP_ALLOWED_NUMBERS=" "$PROJECT_DIR/.env" 2>/dev/null; then
                sed -i.bak "s/^WHATSAPP_ALLOWED_NUMBERS=.*/WHATSAPP_ALLOWED_NUMBERS=$WA_NUMS/" "$PROJECT_DIR/.env"
                rm -f "$PROJECT_DIR/.env.bak"
            else
                echo "WHATSAPP_ALLOWED_NUMBERS=$WA_NUMS" >> "$PROJECT_DIR/.env"
            fi
            export WHATSAPP_ALLOWED_NUMBERS="$WA_NUMS"
            echo "   ✓ WHATSAPP_ALLOWED_NUMBERS=$WA_NUMS"
        else
            echo "   → Allowing all numbers (change later in .env)"
        fi
        echo ""
    fi
fi

echo "Starting bot (BOT_MODE=$BOT_MODE, CLAUDE_MODE=${CLAUDE_MODE:-cli})..."
exec "$PYTHON" "$PROJECT_DIR/main.py"

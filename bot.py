"""
Backward-compatibility shim.

The bot was refactored into:
  main.py         — entry point (BOT_MODE=telegram|whatsapp|both)
  telegram_bot.py — Telegram adapter
  whatsapp_bot.py — WhatsApp adapter (Green API polling)
  claude_core.py  — shared Claude CLI/API + SQLite + rate limiting

Running this file directly still works and starts the Telegram bot,
exactly as before.
"""

import asyncio
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    import os
    os.environ.setdefault("BOT_MODE", "telegram")
    from main import _main
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

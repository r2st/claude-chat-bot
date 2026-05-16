"""
Entry point for the Claude messenger bot.

Set BOT_MODE in your .env:
  BOT_MODE=telegram    — run Telegram bot only  (default)
  BOT_MODE=whatsapp    — run WhatsApp bot only
  BOT_MODE=both        — run both simultaneously
"""

import asyncio
import logging
import os
import threading

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger(__name__)

BOT_MODE = os.getenv("BOT_MODE", "telegram").lower()


def _run_whatsapp_thread():
    from whatsapp_bot import run_whatsapp
    try:
        run_whatsapp()
    except Exception:
        log.exception("WhatsApp bot crashed")


async def _main():
    if BOT_MODE == "telegram":
        log.info("Starting in Telegram-only mode")
        from telegram_bot import run_telegram
        await run_telegram()

    elif BOT_MODE == "whatsapp":
        log.info("Starting in WhatsApp-only mode")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _run_whatsapp_thread)

    elif BOT_MODE == "both":
        log.info("Starting in dual mode (Telegram + WhatsApp)")
        wa_thread = threading.Thread(target=_run_whatsapp_thread, daemon=True)
        wa_thread.start()
        from telegram_bot import run_telegram
        await run_telegram()

    else:
        raise ValueError(
            f"Unknown BOT_MODE={BOT_MODE!r}. "
            "Set BOT_MODE to 'telegram', 'whatsapp', or 'both'."
        )


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("Shutting down.")

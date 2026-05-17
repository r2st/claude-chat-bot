"""
Entry point for the Claude messenger bot.

Set BOT_MODE in your .env to one or more platforms (comma-separated):

  BOT_MODE=telegram              — Telegram only  (default)
  BOT_MODE=whatsapp              — WhatsApp only
  BOT_MODE=slack                 — Slack only
  BOT_MODE=telegram,whatsapp     — Telegram + WhatsApp
  BOT_MODE=telegram,slack        — Telegram + Slack
  BOT_MODE=whatsapp,slack        — WhatsApp + Slack
  BOT_MODE=all                   — all three platforms

Legacy alias: BOT_MODE=both → telegram,whatsapp
"""

import asyncio
import logging
import logging.handlers
import os
import threading

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            "bot.log", maxBytes=5_000_000, backupCount=3
        ),
    ],
)
log = logging.getLogger(__name__)

# ─── Parse BOT_MODE ────────────────────────────────────────────────────────────

_raw_mode = os.getenv("BOT_MODE", "telegram").lower().strip()

# legacy / convenience aliases
_ALIASES = {
    "both": {"telegram", "whatsapp"},
    "all":  {"telegram", "whatsapp", "slack"},
}

if _raw_mode in _ALIASES:
    PLATFORMS: set[str] = _ALIASES[_raw_mode]
else:
    PLATFORMS = {p.strip() for p in _raw_mode.split(",") if p.strip()}

_VALID = {"telegram", "whatsapp", "slack"}
_unknown = PLATFORMS - _VALID
if _unknown:
    raise ValueError(
        f"Unknown platform(s) in BOT_MODE: {_unknown}. "
        f"Valid values: {_VALID} (or 'both', 'all')"
    )

log.info("Platforms enabled: %s", ", ".join(sorted(PLATFORMS)))


# ─── Background thread wrappers ────────────────────────────────────────────────

def _run_whatsapp() -> None:
    from whatsapp_bot import run_whatsapp
    try:
        run_whatsapp()
    except Exception:
        log.exception("WhatsApp bot crashed")


def _run_slack() -> None:
    from slack_bot import run_slack
    try:
        run_slack()
    except Exception:
        log.exception("Slack bot crashed")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def _main() -> None:
    # Start WhatsApp and/or Slack in background threads (they are blocking/sync)
    if "whatsapp" in PLATFORMS:
        threading.Thread(target=_run_whatsapp, daemon=True, name="whatsapp").start()

    if "slack" in PLATFORMS:
        threading.Thread(target=_run_slack, daemon=True, name="slack").start()

    if "telegram" in PLATFORMS:
        # Telegram owns the asyncio event loop
        from telegram_bot import run_telegram
        await run_telegram()
    else:
        # No Telegram — keep the process alive while background threads run
        log.info("Running without Telegram. Press Ctrl-C to stop.")
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("Shutting down.")

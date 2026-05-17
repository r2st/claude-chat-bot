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

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import subprocess
import time
import threading

from dotenv import load_dotenv

load_dotenv()

_debug = os.getenv("TELECHAT_DEBUG", "").lower() in ("1", "true", "yes")
_log_level = logging.DEBUG if _debug else logging.INFO

_console = logging.StreamHandler()
_console.setLevel(logging.WARNING if not _debug else logging.DEBUG)

_file = logging.handlers.RotatingFileHandler(
    "bot.log", maxBytes=5_000_000, backupCount=3
)
_file.setLevel(_log_level)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[_console, _file],
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
    from .whatsapp_bot import run_whatsapp
    try:
        run_whatsapp()
    except Exception:
        log.exception("WhatsApp bot crashed")


def _run_slack() -> None:
    from .slack_bot import run_slack
    try:
        run_slack()
    except Exception:
        log.exception("Slack bot crashed")


# ─── Kill existing instances ──────────────────────────────────────────────────

def _kill_existing():
    """Kill any other telechat processes to avoid Telegram 409 conflicts."""
    my_pid = os.getpid()
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "telechat_pkg.main"], text=True
        ).strip()
        for line in out.splitlines():
            pid = int(line.strip())
            if pid != my_pid:
                os.kill(pid, signal.SIGTERM)
                log.info("Killed existing telechat process (PID %d)", pid)
    except (subprocess.CalledProcessError, ValueError):
        pass
    # Also kill via port 8484 (health server)
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", ":8484"], text=True
        ).strip()
        for line in out.splitlines():
            pid = int(line.strip())
            if pid != my_pid:
                os.kill(pid, signal.SIGTERM)
    except (subprocess.CalledProcessError, ValueError):
        pass


# ─── Main ─────────────────────────────────────────────────────────────────────

async def _main() -> None:
    _kill_existing()
    await asyncio.sleep(1)  # let Telegram release the getUpdates connection

    # Always print startup info to console regardless of log level
    platforms = ", ".join(sorted(PLATFORMS))
    print(f"telechat — {platforms}")
    if _debug:
        print("Debug mode ON (verbose logging)")

    # Start WhatsApp and/or Slack in background threads (they are blocking/sync)
    if "whatsapp" in PLATFORMS:
        threading.Thread(target=_run_whatsapp, daemon=True, name="whatsapp").start()

    if "slack" in PLATFORMS:
        threading.Thread(target=_run_slack, daemon=True, name="slack").start()

    if "telegram" in PLATFORMS:
        # Telegram owns the asyncio event loop
        from .telegram_bot import run_telegram
        await run_telegram()
    else:
        # No Telegram — keep the process alive while background threads run
        log.info("Running without Telegram. Press Ctrl-C to stop.")
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass


_sigint_count = 0


def _sigint_handler(sig, frame):
    global _sigint_count
    _sigint_count += 1
    if _sigint_count == 1:
        print("\nShutting down…")
        os._exit(0)
    else:
        os._exit(1)


def cli_entry():
    """Entry point for `pip install telechat` → `telechat` command."""
    signal.signal(signal.SIGINT, _sigint_handler)
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nShutting down…")
        os._exit(0)


if __name__ == "__main__":
    cli_entry()

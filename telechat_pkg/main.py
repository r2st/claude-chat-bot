"""
Entry point for the Claude messenger bot.

Subcommands:
  telechat              — start the bot (default)
  telechat init         — interactive .env setup
  telechat start        — start the bot

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

import os
import re
import signal
import sys


# ─── .env helpers ─────────────────────────────────────────────────────────────

def _find_env_file() -> str:
    """Return path to .env, searching cwd then package dir."""
    cwd_env = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(cwd_env):
        return cwd_env
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    proj_env = os.path.join(os.path.dirname(pkg_dir), ".env")
    if os.path.isfile(proj_env):
        return proj_env
    return cwd_env  # default location for creation


def _read_env(path: str) -> dict[str, str]:
    """Read a .env file into a dict (ignores comments, blank lines)."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _set_env_var(path: str, key: str, value: str) -> None:
    """Set a variable in the .env file, updating in-place or appending."""
    lines: list[str] = []
    found = False
    if os.path.isfile(path):
        with open(path) as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                lines[i] = f"{key}={value}\n"
                found = True
                break
    if not found:
        lines.append(f"{key}={value}\n")
    with open(path, "w") as f:
        f.writelines(lines)


def _env_example_path() -> str | None:
    """Find .env.example in the project."""
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    proj_dir = os.path.dirname(pkg_dir)
    for d in [os.getcwd(), proj_dir]:
        p = os.path.join(d, ".env.example")
        if os.path.isfile(p):
            return p
    return None


# ─── Init command ─────────────────────────────────────────────────────────────

def _cmd_init() -> None:
    """Interactive .env setup wizard."""
    import shutil

    env_path = _find_env_file()
    exists = os.path.isfile(env_path)

    if not exists:
        example = _env_example_path()
        if example:
            shutil.copy2(example, env_path)
            print(f"Created {env_path} from template")
        else:
            open(env_path, "w").close()
            print(f"Created {env_path}")
    else:
        print(f"Using {env_path}")

    env = _read_env(env_path)

    # ── Platform selection ────────────────────────────────────────────────
    current_mode = env.get("BOT_MODE", "telegram")
    print(f"\n── Platforms ({'current: ' + current_mode}) ──")
    print("  1) telegram")
    print("  2) whatsapp")
    print("  3) slack")
    print("  4) telegram,whatsapp")
    print("  5) telegram,slack")
    print("  6) all (telegram + whatsapp + slack)")
    choice = input(f"\nChoose platforms [enter to keep '{current_mode}']: ").strip()
    mode_map = {"1": "telegram", "2": "whatsapp", "3": "slack",
                "4": "telegram,whatsapp", "5": "telegram,slack", "6": "all"}
    if choice in mode_map:
        current_mode = mode_map[choice]
        _set_env_var(env_path, "BOT_MODE", current_mode)
        print(f"  → BOT_MODE={current_mode}")
    elif choice:
        current_mode = choice
        _set_env_var(env_path, "BOT_MODE", current_mode)
        print(f"  → BOT_MODE={current_mode}")

    platforms = _parse_platforms(current_mode)

    # ── Telegram setup ────────────────────────────────────────────────────
    if "telegram" in platforms:
        print("\n── Telegram ──")
        current = env.get("TELEGRAM_BOT_TOKEN", "")
        if current and current != "your_telegram_bot_token":
            print(f"  Token: {current[:10]}...{current[-4:]}")
            if input("  Change? [y/N]: ").strip().lower() != "y":
                current = ""
        if not current or current == "your_telegram_bot_token":
            token = input("  Bot token (from @BotFather): ").strip()
            if token:
                _set_env_var(env_path, "TELEGRAM_BOT_TOKEN", token)
                print("  → saved")

        current_ids = env.get("ALLOWED_USER_IDS", "")
        print(f"  Allowed user IDs: {current_ids or '(everyone)'}")
        ids = input("  Telegram user IDs (comma-sep, enter to keep, 'none' for all): ").strip()
        if ids.lower() == "none":
            _set_env_var(env_path, "ALLOWED_USER_IDS", "")
        elif ids:
            _set_env_var(env_path, "ALLOWED_USER_IDS", ids)

    # ── WhatsApp setup ────────────────────────────────────────────────────
    if "whatsapp" in platforms:
        print("\n── WhatsApp (Green API) ──")
        print("  Sign up free: https://console.green-api.com")

        current = env.get("GREEN_API_INSTANCE_ID", "")
        if current:
            print(f"  Instance ID: {current}")
            if input("  Change? [y/N]: ").strip().lower() == "y":
                current = ""
        if not current:
            val = input("  Instance ID: ").strip()
            if val:
                _set_env_var(env_path, "GREEN_API_INSTANCE_ID", val)

        current = env.get("GREEN_API_TOKEN", "")
        if current:
            print(f"  API Token: {current[:8]}...{current[-4:]}")
            if input("  Change? [y/N]: ").strip().lower() == "y":
                current = ""
        if not current:
            val = input("  API Token: ").strip()
            if val:
                _set_env_var(env_path, "GREEN_API_TOKEN", val)

        # ── WhatsApp allowed numbers ──────────────────────────────────
        current_nums = env.get("WHATSAPP_ALLOWED_NUMBERS", "")
        print(f"\n  Allowed WhatsApp numbers: {current_nums or '(everyone)'}")
        print("  Format: country code + number, no '+' or spaces")
        print("  Example: 919876543210,14155552671")
        print("  Tip: send !id to the bot to discover your number")
        nums = input("  Numbers (comma-sep, enter to keep, 'none' for all): ").strip()
        if nums.lower() == "none":
            _set_env_var(env_path, "WHATSAPP_ALLOWED_NUMBERS", "")
            print("  → allowing everyone")
        elif nums:
            clean = re.sub(r"[\s+\-()]", "", nums)
            _set_env_var(env_path, "WHATSAPP_ALLOWED_NUMBERS", clean)
            print(f"  → WHATSAPP_ALLOWED_NUMBERS={clean}")

    # ── Slack setup ───────────────────────────────────────────────────────
    if "slack" in platforms:
        print("\n── Slack ──")
        print("  Create app: https://api.slack.com/apps")

        current = env.get("SLACK_BOT_TOKEN", "")
        if current and not current.startswith("xoxb-"):
            current = ""
        if current:
            print(f"  Bot Token: {current[:10]}...{current[-4:]}")
            if input("  Change? [y/N]: ").strip().lower() == "y":
                current = ""
        if not current:
            val = input("  Bot Token (xoxb-...): ").strip()
            if val:
                _set_env_var(env_path, "SLACK_BOT_TOKEN", val)

        current = env.get("SLACK_APP_TOKEN", "")
        if current and not current.startswith("xapp-"):
            current = ""
        if current:
            print(f"  App Token: {current[:10]}...{current[-4:]}")
            if input("  Change? [y/N]: ").strip().lower() == "y":
                current = ""
        if not current:
            val = input("  App Token (xapp-...): ").strip()
            if val:
                _set_env_var(env_path, "SLACK_APP_TOKEN", val)

        current_ids = env.get("SLACK_ALLOWED_USER_IDS", "")
        print(f"  Allowed Slack user IDs: {current_ids or '(everyone)'}")
        ids = input("  Slack member IDs (comma-sep, enter to keep, 'none' for all): ").strip()
        if ids.lower() == "none":
            _set_env_var(env_path, "SLACK_ALLOWED_USER_IDS", "")
        elif ids:
            _set_env_var(env_path, "SLACK_ALLOWED_USER_IDS", ids)

    # ── Claude settings ───────────────────────────────────────────────────
    print("\n── Claude ──")
    current_cmode = env.get("CLAUDE_MODE", "cli")
    print(f"  Mode: {current_cmode} (cli = Claude Code CLI, api = Anthropic API)")
    cmode = input(f"  Claude mode [enter to keep '{current_cmode}']: ").strip().lower()
    if cmode in ("cli", "api"):
        _set_env_var(env_path, "CLAUDE_MODE", cmode)
        current_cmode = cmode

    if current_cmode == "api":
        current_key = env.get("ANTHROPIC_API_KEY", "")
        if current_key and current_key != "your_api_key_here":
            print(f"  API Key: {current_key[:8]}...{current_key[-4:]}")
        else:
            key = input("  Anthropic API key: ").strip()
            if key:
                _set_env_var(env_path, "ANTHROPIC_API_KEY", key)

    print(f"\nDone! Config saved to {env_path}")
    print("Run 'telechat' or 'telechat start' to launch the bot.")


def _parse_platforms(mode: str) -> set[str]:
    aliases = {"both": {"telegram", "whatsapp"}, "all": {"telegram", "whatsapp", "slack"}}
    mode = mode.lower().strip()
    if mode in aliases:
        return aliases[mode]
    return {p.strip() for p in mode.split(",") if p.strip()}


# ─── Start command (heavy setup deferred here) ───────────────────────────────

def _cmd_start() -> None:
    """Load config and start the bot."""
    import asyncio
    import logging
    import logging.handlers
    import subprocess
    import threading
    import time

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

    # ── Parse BOT_MODE ────────────────────────────────────────────────────
    _raw_mode = os.getenv("BOT_MODE", "telegram").lower().strip()
    _ALIASES = {"both": {"telegram", "whatsapp"}, "all": {"telegram", "whatsapp", "slack"}}

    if _raw_mode in _ALIASES:
        PLATFORMS: set[str] = _ALIASES[_raw_mode]
    else:
        PLATFORMS = {p.strip() for p in _raw_mode.split(",") if p.strip()}

    _VALID = {"telegram", "whatsapp", "slack"}
    _unknown = PLATFORMS - _VALID
    if _unknown:
        print(f"ERROR: Unknown platform(s) in BOT_MODE: {_unknown}")
        print(f"Valid values: {_VALID} (or 'both', 'all')")
        print("Run 'telechat init' to configure.")
        sys.exit(1)

    log.info("Platforms enabled: %s", ", ".join(sorted(PLATFORMS)))

    # ── WhatsApp pre-flight check ─────────────────────────────────────────
    if "whatsapp" in PLATFORMS:
        wa_nums = os.getenv("WHATSAPP_ALLOWED_NUMBERS", "").strip()
        if not wa_nums:
            print("Note: WHATSAPP_ALLOWED_NUMBERS is empty — anyone can message the bot.")
            print("      Send !id to the bot to find your number, then run 'telechat init'")
            print("      or set WHATSAPP_ALLOWED_NUMBERS in .env to restrict access.\n")
        gid = os.getenv("GREEN_API_INSTANCE_ID", "").strip()
        gtk = os.getenv("GREEN_API_TOKEN", "").strip()
        if not gid or not gtk:
            print("ERROR: WhatsApp enabled but GREEN_API_INSTANCE_ID / GREEN_API_TOKEN not set.")
            print("Run 'telechat init' to configure.")
            sys.exit(1)

    # ── Kill existing instances ───────────────────────────────────────────
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
    try:
        out = subprocess.check_output(["lsof", "-ti", ":8484"], text=True).strip()
        for line in out.splitlines():
            pid = int(line.strip())
            if pid != my_pid:
                os.kill(pid, signal.SIGTERM)
    except (subprocess.CalledProcessError, ValueError):
        pass

    # ── Background thread wrappers ────────────────────────────────────────
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

    # ── Async main ────────────────────────────────────────────────────────
    async def _main() -> None:
        await asyncio.sleep(1)

        platforms = ", ".join(sorted(PLATFORMS))
        print(f"telechat — {platforms}")
        if _debug:
            print("Debug mode ON (verbose logging)")

        if "whatsapp" in PLATFORMS:
            threading.Thread(target=_run_whatsapp, daemon=True, name="whatsapp").start()

        if "slack" in PLATFORMS:
            threading.Thread(target=_run_slack, daemon=True, name="slack").start()

        if "telegram" in PLATFORMS:
            from .telegram_bot import run_telegram
            await run_telegram()
        else:
            log.info("Running without Telegram. Press Ctrl-C to stop.")
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nShutting down…")
        os._exit(0)


# ─── CLI entry point ─────────────────────────────────────────────────────────

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

    args = sys.argv[1:]
    cmd = args[0] if args else "start"

    if cmd == "init":
        _cmd_init()
    elif cmd in ("start", "run"):
        _cmd_start()
    elif cmd in ("-h", "--help", "help"):
        print("Usage: telechat [command]")
        print()
        print("Commands:")
        print("  init     Interactive setup wizard (creates/updates .env)")
        print("  start    Start the bot (default)")
        print("  help     Show this help")
        print()
        print("Examples:")
        print("  telechat init          # configure platforms & credentials")
        print("  telechat               # start the bot")
        print("  telechat start         # same as above")
    elif cmd == "--version":
        try:
            from importlib.metadata import version
            print(f"telechat {version('telechatai')}")
        except Exception:
            print("telechat (unknown version)")
    else:
        print(f"Unknown command: {cmd}")
        print("Run 'telechat help' for usage.")
        sys.exit(1)


if __name__ == "__main__":
    cli_entry()

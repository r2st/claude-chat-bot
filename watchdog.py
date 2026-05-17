"""
Self-improving watchdog — monitors bot logs, detects errors, and uses
Claude Code CLI to analyze + fix issues automatically.

Runs as a separate LaunchD service alongside the main bot.

Safety:
  - Fixes are made on a temporary git branch
  - Python syntax is verified before merging
  - The bot is restarted and logs are monitored for regression
  - If the fix causes MORE errors, it's auto-reverted
  - Same error is never re-attempted within a cooldown window
  - Max fixes per hour is capped
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("watchdog")

# ─── Config ──────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(os.getenv("WATCHDOG_PROJECT_DIR", "/Users/dev/projects/claude-chat-bot"))
BOT_LOG = PROJECT_DIR / "bot.log"
WATCHDOG_LOG = PROJECT_DIR / "watchdog.log"
WATCHDOG_STATE = PROJECT_DIR / ".watchdog_state.json"

SCAN_INTERVAL = int(os.getenv("WATCHDOG_SCAN_INTERVAL", "30"))
ERROR_BATCH_WINDOW = int(os.getenv("WATCHDOG_BATCH_WINDOW", "60"))
MAX_FIXES_PER_HOUR = int(os.getenv("WATCHDOG_MAX_FIXES_HOUR", "3"))
FIX_COOLDOWN = int(os.getenv("WATCHDOG_FIX_COOLDOWN", "1800"))  # 30 min
REGRESSION_WATCH = int(os.getenv("WATCHDOG_REGRESSION_WATCH", "120"))  # 2 min
BOT_SERVICE = os.getenv("WATCHDOG_BOT_SERVICE", "com.claude.chat-bot")
CLAUDE_MODEL = os.getenv("WATCHDOG_CLAUDE_MODEL", "sonnet")
ENABLED = os.getenv("WATCHDOG_ENABLED", "true").lower() == "true"
DRY_RUN = os.getenv("WATCHDOG_DRY_RUN", "false").lower() == "true"

# Patterns to ignore (noisy but harmless)
IGNORE_PATTERNS = [
    r"Green API DELETE",
    r"HTTP Request: POST.*getUpdates",
    r"httpx.*HTTP/1\.1 200 OK",
    r"Telegram bot (starting|running)",
    r"Platforms enabled:",
    r"message is not modified",
]

# ─── Data types ──────────────────────────────────────────────────────────────

@dataclass
class ErrorEvent:
    timestamp: float
    level: str  # ERROR, EXCEPTION, WARNING
    logger: str
    message: str
    traceback: str = ""
    fingerprint: str = ""

    def __post_init__(self):
        if not self.fingerprint:
            key = f"{self.logger}:{self._core_message()}"
            self.fingerprint = hashlib.sha256(key.encode()).hexdigest()[:12]

    def _core_message(self) -> str:
        msg = self.message
        # Strip variable parts: numbers, paths, timestamps
        msg = re.sub(r'\d+', 'N', msg)
        msg = re.sub(r'/[\w/.-]+', '/PATH', msg)
        return msg[:200]


@dataclass
class FixAttempt:
    fingerprint: str
    timestamp: float
    branch: str
    commit_sha: str = ""
    success: bool = False
    reverted: bool = False
    description: str = ""


@dataclass
class WatchdogState:
    last_read_pos: int = 0
    fix_attempts: list[FixAttempt] = field(default_factory=list)
    cooldowns: dict[str, float] = field(default_factory=dict)  # fingerprint → last attempt time
    fixes_this_hour: list[float] = field(default_factory=list)  # timestamps

    def save(self):
        data = {
            "last_read_pos": self.last_read_pos,
            "cooldowns": self.cooldowns,
            "fixes_this_hour": self.fixes_this_hour,
            "fix_attempts": [
                {
                    "fingerprint": f.fingerprint,
                    "timestamp": f.timestamp,
                    "branch": f.branch,
                    "commit_sha": f.commit_sha,
                    "success": f.success,
                    "reverted": f.reverted,
                    "description": f.description,
                }
                for f in self.fix_attempts[-50:]  # keep last 50
            ],
        }
        WATCHDOG_STATE.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> "WatchdogState":
        if not WATCHDOG_STATE.exists():
            return cls()
        try:
            data = json.loads(WATCHDOG_STATE.read_text())
            state = cls(
                last_read_pos=data.get("last_read_pos", 0),
                cooldowns=data.get("cooldowns", {}),
                fixes_this_hour=data.get("fixes_this_hour", []),
            )
            for fa in data.get("fix_attempts", []):
                state.fix_attempts.append(FixAttempt(**fa))
            return state
        except Exception:
            return cls()

    def can_attempt_fix(self, fingerprint: str) -> tuple[bool, str]:
        now = time.time()
        # Check cooldown
        last = self.cooldowns.get(fingerprint, 0)
        if now - last < FIX_COOLDOWN:
            remaining = int(FIX_COOLDOWN - (now - last))
            return False, f"cooldown ({remaining}s remaining)"
        # Check hourly limit
        self.fixes_this_hour = [t for t in self.fixes_this_hour if now - t < 3600]
        if len(self.fixes_this_hour) >= MAX_FIXES_PER_HOUR:
            return False, f"hourly limit ({MAX_FIXES_PER_HOUR}/hr)"
        return True, ""

    def record_attempt(self, fingerprint: str):
        now = time.time()
        self.cooldowns[fingerprint] = now
        self.fixes_this_hour.append(now)


# ─── Log parser ──────────────────────────────────────────────────────────────

_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
    r"\s+\[(\w+)\]\s+"
    r"([\w._]+)\s+[—-]\s+"
    r"(.+)$"
)
_TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\):")
_COMPILED_IGNORE = [re.compile(p) for p in IGNORE_PATTERNS]


def _should_ignore(line: str) -> bool:
    return any(p.search(line) for p in _COMPILED_IGNORE)


def parse_log_tail(state: WatchdogState) -> list[ErrorEvent]:
    if not BOT_LOG.exists():
        return []

    file_size = BOT_LOG.stat().st_size
    if file_size < state.last_read_pos:
        state.last_read_pos = 0  # log was rotated

    errors: list[ErrorEvent] = []
    current_tb_lines: list[str] = []
    in_traceback = False
    current_event: ErrorEvent | None = None

    with open(BOT_LOG, "r") as f:
        f.seek(state.last_read_pos)
        for line in f:
            line = line.rstrip("\n")

            if _should_ignore(line):
                continue

            if _TRACEBACK_START.match(line):
                in_traceback = True
                current_tb_lines = [line]
                continue

            if in_traceback:
                if line.startswith("  ") or line.startswith("\t") or not line:
                    current_tb_lines.append(line)
                    continue
                else:
                    # Traceback ended — this line is the exception message
                    current_tb_lines.append(line)
                    tb_text = "\n".join(current_tb_lines)
                    if current_event:
                        current_event.traceback = tb_text
                    in_traceback = False
                    current_tb_lines = []
                    continue

            m = _LOG_RE.match(line)
            if not m:
                continue

            ts_str, level, logger, message = m.groups()
            if level not in ("ERROR", "CRITICAL"):
                continue

            current_event = ErrorEvent(
                timestamp=time.time(),
                level=level,
                logger=logger,
                message=message,
            )
            errors.append(current_event)

        state.last_read_pos = f.tell()

    return errors


def deduplicate_errors(errors: list[ErrorEvent]) -> dict[str, list[ErrorEvent]]:
    groups: dict[str, list[ErrorEvent]] = defaultdict(list)
    for e in errors:
        groups[e.fingerprint].append(e)
    return groups


# ─── Git operations ──────────────────────────────────────────────────────────

def _git(*args, cwd=None) -> tuple[int, str]:
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True,
        cwd=cwd or PROJECT_DIR,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _current_branch() -> str:
    rc, out = _git("branch", "--show-current")
    return out if rc == 0 else "main"


def _create_fix_branch(fingerprint: str) -> str:
    branch = f"watchdog/fix-{fingerprint}-{int(time.time())}"
    _git("checkout", "-b", branch)
    return branch


def _merge_and_cleanup(branch: str) -> bool:
    _git("checkout", "main")
    rc, out = _git("merge", branch, "--no-edit")
    if rc != 0:
        log.error("Merge failed: %s", out)
        _git("merge", "--abort")
        _git("branch", "-D", branch)
        return False
    _git("branch", "-d", branch)
    return True


def _revert_last_commit():
    _git("revert", "HEAD", "--no-edit")


def _push():
    _git("push", "origin", "main")


# ─── Code validation ─────────────────────────────────────────────────────────

def _validate_python() -> tuple[bool, str]:
    errors = []
    for py_file in PROJECT_DIR.glob("*.py"):
        try:
            import ast
            ast.parse(py_file.read_text())
        except SyntaxError as e:
            errors.append(f"{py_file.name}:{e.lineno}: {e.msg}")
    return len(errors) == 0, "; ".join(errors)


# ─── Claude Code integration ─────────────────────────────────────────────────

async def _ask_claude_fix(error_group: list[ErrorEvent]) -> tuple[str, bool]:
    primary = error_group[0]
    count = len(error_group)

    prompt = f"""You are a code maintenance bot. Analyze this error from a Python Telegram bot and fix it.

ERROR ({count} occurrence(s)):
Logger: {primary.logger}
Message: {primary.message}
{"Traceback:" + chr(10) + primary.traceback if primary.traceback else ""}

RULES:
- Only modify files that directly relate to this error
- Make minimal, targeted fixes — don't refactor or add features
- Ensure the fix handles the root cause, not just symptoms
- If you're not confident in a fix, explain why and don't change anything
- After fixing, verify the Python syntax is valid

The project is a Python Telegram bot with these key files:
- telegram_bot.py — Telegram adapter
- claude_core.py — shared Claude CLI/API/SDK integration + DB layer
- whatsapp_bot.py — WhatsApp adapter
- slack_bot.py — Slack adapter
- main.py — entry point

Fix the error. If you can't confidently fix it, respond with just "SKIP: <reason>"."""

    cmd = [
        "claude",
        "--model", CLAUDE_MODEL,
        "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_DIR),
            limit=10 * 1024 * 1024,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=300,
        )
        output = stdout.decode().strip()

        # Parse JSON output
        try:
            result = json.loads(output)
            text = result.get("result", output)
        except json.JSONDecodeError:
            text = output

        if text.startswith("SKIP:"):
            return text, False

        return text, True

    except asyncio.TimeoutError:
        return "SKIP: Claude timed out", False
    except Exception as e:
        return f"SKIP: {e}", False


# ─── Bot service management ──────────────────────────────────────────────────

def _restart_bot():
    log.info("Restarting bot service: %s", BOT_SERVICE)
    subprocess.run(["launchctl", "stop", BOT_SERVICE], capture_output=True)
    time.sleep(3)
    subprocess.run(["launchctl", "start", BOT_SERVICE], capture_output=True)
    time.sleep(5)


def _count_recent_errors(seconds: int = 60) -> int:
    if not BOT_LOG.exists():
        return 0
    cutoff_size = BOT_LOG.stat().st_size
    count = 0
    with open(BOT_LOG, "r") as f:
        # Read last 50KB for quick check
        f.seek(max(0, cutoff_size - 50000))
        for line in f:
            if "[ERROR]" in line or "[CRITICAL]" in line:
                if not _should_ignore(line):
                    count += 1
    return count


# ─── Fix pipeline ────────────────────────────────────────────────────────────

async def attempt_fix(
    fingerprint: str,
    error_group: list[ErrorEvent],
    state: WatchdogState,
) -> FixAttempt:
    primary = error_group[0]
    log.info(
        "Attempting fix for [%s] %s (%d occurrences)",
        fingerprint, primary.message[:80], len(error_group),
    )

    original_branch = _current_branch()
    branch = _create_fix_branch(fingerprint)
    attempt = FixAttempt(
        fingerprint=fingerprint,
        timestamp=time.time(),
        branch=branch,
    )

    try:
        # Step 1: Ask Claude to fix
        description, made_changes = await _ask_claude_fix(error_group)
        attempt.description = description[:500]

        if not made_changes:
            log.info("Claude skipped fix: %s", description[:200])
            _git("checkout", original_branch)
            _git("branch", "-D", branch)
            return attempt

        # Step 2: Check if any files were modified
        rc, diff = _git("diff", "--name-only")
        if not diff.strip():
            log.info("No files changed — skipping")
            _git("checkout", original_branch)
            _git("branch", "-D", branch)
            return attempt

        changed_files = diff.strip().split("\n")
        log.info("Files changed: %s", ", ".join(changed_files))

        # Step 3: Validate Python syntax
        valid, syntax_err = _validate_python()
        if not valid:
            log.error("Syntax validation failed: %s", syntax_err)
            _git("checkout", ".")  # discard changes
            _git("checkout", original_branch)
            _git("branch", "-D", branch)
            return attempt

        if DRY_RUN:
            log.info("[DRY RUN] Would commit and deploy fix for [%s]", fingerprint)
            _git("checkout", ".")
            _git("checkout", original_branch)
            _git("branch", "-D", branch)
            attempt.success = True
            return attempt

        # Step 4: Commit
        _git("add", "-A")
        commit_msg = (
            f"fix(watchdog): auto-fix {primary.logger} — {primary.message[:60]}\n\n"
            f"Error fingerprint: {fingerprint}\n"
            f"Occurrences: {len(error_group)}\n"
            f"Fix: {description[:200]}\n\n"
            f"Co-Authored-By: Watchdog Bot <watchdog@bot>"
        )
        rc, out = _git("commit", "-m", commit_msg)
        if rc != 0:
            log.error("Commit failed: %s", out)
            _git("checkout", original_branch)
            _git("branch", "-D", branch)
            return attempt

        # Get commit SHA
        _, sha = _git("rev-parse", "HEAD")
        attempt.commit_sha = sha[:8]

        # Step 5: Merge to main
        if not _merge_and_cleanup(branch):
            return attempt

        # Step 6: Restart bot and monitor for regression
        errors_before = _count_recent_errors(30)
        _restart_bot()

        log.info("Monitoring for regression (%ds)...", REGRESSION_WATCH)
        await asyncio.sleep(REGRESSION_WATCH)

        errors_after = _count_recent_errors(REGRESSION_WATCH)

        if errors_after > errors_before + 3:
            log.warning(
                "Regression detected! errors before=%d after=%d. Reverting.",
                errors_before, errors_after,
            )
            _revert_last_commit()
            _restart_bot()
            attempt.reverted = True
            attempt.description += " [REVERTED: regression]"
        else:
            attempt.success = True
            log.info("Fix verified — no regression detected")
            _push()

    except Exception as e:
        log.exception("Fix attempt failed: %s", e)
        # Make sure we're back on the original branch
        try:
            _git("checkout", ".")
            _git("checkout", original_branch)
            _git("branch", "-D", branch)
        except Exception:
            pass

    return attempt


# ─── Main loop ───────────────────────────────────────────────────────────────

async def run_watchdog():
    log.info(
        "Watchdog starting — scanning %s every %ds (dry_run=%s)",
        BOT_LOG, SCAN_INTERVAL, DRY_RUN,
    )

    state = WatchdogState.load()

    while True:
        try:
            # Parse new log entries
            errors = parse_log_tail(state)

            if errors:
                groups = deduplicate_errors(errors)
                log.info("Found %d error(s) in %d group(s)", len(errors), len(groups))

                for fingerprint, group in groups.items():
                    can_fix, reason = state.can_attempt_fix(fingerprint)
                    if not can_fix:
                        log.debug("Skipping [%s]: %s", fingerprint, reason)
                        continue

                    state.record_attempt(fingerprint)
                    attempt = await attempt_fix(fingerprint, group, state)
                    state.fix_attempts.append(attempt)

                    if attempt.success:
                        log.info(
                            "✅ Fixed [%s]: %s",
                            fingerprint, attempt.description[:100],
                        )
                    elif attempt.reverted:
                        log.warning("↩️ Reverted fix for [%s]", fingerprint)

            state.save()

        except Exception:
            log.exception("Watchdog scan cycle failed")

        await asyncio.sleep(SCAN_INTERVAL)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(WATCHDOG_LOG)),
        ],
    )

    if not ENABLED:
        log.info("Watchdog is disabled (WATCHDOG_ENABLED=false)")
        return

    asyncio.run(run_watchdog())


if __name__ == "__main__":
    main()

"""
Shared Claude invocation + conversation DB + rate limiting.

Used by both telegram_bot.py and whatsapp_bot.py.
"""

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ─── Config (read once; adapters may override per-user) ────────────────────────

CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "sonnet")
CLAUDE_SYSTEM     = os.getenv(
    "CLAUDE_SYSTEM_PROMPT",
    "You are a helpful AI assistant. Be concise unless asked for detail.",
)
CLAUDE_ADD_DIRS   = os.getenv("CLAUDE_ADD_DIRS", "")
CLAUDE_WORK_DIR   = os.getenv("CLAUDE_CLI_WORK_DIR", os.path.expanduser("~"))
CLAUDE_TIMEOUT    = int(os.getenv("CLAUDE_TIMEOUT", "180"))
CLAUDE_MODE       = os.getenv("CLAUDE_MODE", "cli")   # cli | api | sdk
CLAUDE_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_API_MODEL  = os.getenv("CLAUDE_API_MODEL", "claude-sonnet-4-20250514")
CLAUDE_MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))
CLAUDE_PERM_MODE  = os.getenv("CLAUDE_CLI_PERMISSION_MODE", "auto")

RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW   = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "bot.db"))

# ─── Rate limiting ──────────────────────────────────────────────────────────────

_rate_state: dict[str, list[float]] = {}


def check_rate_limit(key: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.time()
    bucket = _rate_state.setdefault(key, [])
    _rate_state[key] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_state[key]) >= RATE_LIMIT_REQUESTS:
        return False
    _rate_state[key].append(now)
    return True


# ─── SQLite conversation store ──────────────────────────────────────────────────

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)

    # Enable WAL mode for better concurrent access
    conn.execute("PRAGMA journal_mode=WAL")

    # Migrate from old schema (no platform column) if needed
    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if cols and "platform" not in cols:
        log.info("Migrating database to multi-platform schema…")
        conn.execute("ALTER TABLE conversations RENAME TO _conv_old")
        conn.execute("""
            CREATE TABLE conversations (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                ts REAL NOT NULL, PRIMARY KEY (platform, user_id, ts))
        """)
        conn.execute("""
            INSERT INTO conversations (platform, user_id, role, content, ts)
            SELECT 'telegram', CAST(user_id AS TEXT), role, content, timestamp FROM _conv_old
        """)
        conn.execute("DROP TABLE _conv_old")

        conn.execute("ALTER TABLE usage RENAME TO _usage_old")
        conn.execute("""
            CREATE TABLE usage (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, PRIMARY KEY (platform, user_id))
        """)
        conn.execute("""
            INSERT INTO usage (platform, user_id, message_count, input_tokens, output_tokens)
            SELECT 'telegram', CAST(user_id AS TEXT), message_count, total_input_tokens, total_output_tokens FROM _usage_old
        """)
        conn.execute("DROP TABLE _usage_old")
        conn.commit()
        log.info("Database migration complete.")
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                platform  TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                ts REAL NOT NULL, PRIMARY KEY (platform, user_id, ts))
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, PRIMARY KEY (platform, user_id))
        """)
        conn.commit()

    # Enhanced tables: tool_usage, cost_tracking, sessions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            success INTEGER DEFAULT 1,
            ts REAL NOT NULL)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cost_tracking (
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            requests INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            PRIMARY KEY (platform, user_id, date))
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT,
            engine TEXT DEFAULT 'cli',
            model TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            total_cost_usd REAL DEFAULT 0,
            num_turns INTEGER DEFAULT 0)
    """)
    conn.commit()
    conn.close()


def load_history(platform: str, user_id: str, limit: int = 20) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT role, content FROM conversations
           WHERE platform=? AND user_id=?
           ORDER BY ts DESC LIMIT ?""",
        (platform, user_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def save_turn(platform: str, user_id: str, user_text: str, reply: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO conversations (platform,user_id,role,content,ts) VALUES (?,?,?,?,?)",
        (platform, user_id, "user", user_text, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO conversations (platform,user_id,role,content,ts) VALUES (?,?,?,?,?)",
        (platform, user_id, "assistant", reply, now + 0.001),
    )
    # Keep last 20 messages per user per platform
    count = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE platform=? AND user_id=?",
        (platform, user_id),
    ).fetchone()[0]
    if count > 20:
        conn.execute(
            """DELETE FROM conversations WHERE platform=? AND user_id=? AND ts IN (
               SELECT ts FROM conversations WHERE platform=? AND user_id=?
               ORDER BY ts LIMIT ?)""",
            (platform, user_id, platform, user_id, count - 20),
        )
    conn.commit()
    conn.close()


def clear_history(platform: str, user_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM conversations WHERE platform=? AND user_id=?",
        (platform, user_id),
    )
    conn.commit()
    conn.close()


def track_usage(platform: str, user_id: str, in_tok: int = 0, out_tok: int = 0) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO usage (platform, user_id, message_count, input_tokens, output_tokens)
           VALUES (?,?,1,?,?)
           ON CONFLICT(platform,user_id) DO UPDATE SET
               message_count = message_count + 1,
               input_tokens  = input_tokens  + excluded.input_tokens,
               output_tokens = output_tokens + excluded.output_tokens""",
        (platform, user_id, in_tok, out_tok),
    )
    conn.commit()
    conn.close()


def get_usage(platform: str, user_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT message_count, input_tokens, output_tokens FROM usage WHERE platform=? AND user_id=?",
        (platform, user_id),
    ).fetchone()
    conn.close()
    return {"messages": row[0], "input": row[1], "output": row[2]} if row else {"messages": 0, "input": 0, "output": 0}


def track_tool_usage(platform: str, user_id: str, tools: list[str]) -> None:
    """Record tool usage for analytics."""
    if not tools:
        return
    conn = sqlite3.connect(DB_PATH)
    now = time.time()
    for tool in tools:
        conn.execute(
            "INSERT INTO tool_usage (platform, user_id, tool_name, ts) VALUES (?,?,?,?)",
            (platform, user_id, tool, now),
        )
    conn.commit()
    conn.close()


def track_cost(platform: str, user_id: str, in_tok: int, out_tok: int, cost_usd: float) -> None:
    """Track daily cost for analytics."""
    from datetime import date
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO cost_tracking (platform, user_id, date, requests, input_tokens, output_tokens, cost_usd)
           VALUES (?,?,?,1,?,?,?)
           ON CONFLICT(platform, user_id, date) DO UPDATE SET
               requests = requests + 1,
               input_tokens = input_tokens + excluded.input_tokens,
               output_tokens = output_tokens + excluded.output_tokens,
               cost_usd = cost_usd + excluded.cost_usd""",
        (platform, user_id, today, in_tok, out_tok, cost_usd),
    )
    conn.commit()
    conn.close()


# ─── Claude CLI (sync — for WhatsApp threading model) ──────────────────────────

def ask_claude_sync(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_MODEL,
    system: str = CLAUDE_SYSTEM,
    add_dirs: str = CLAUDE_ADD_DIRS,
    perm_mode: str = CLAUDE_PERM_MODE,
    timeout: int = CLAUDE_TIMEOUT,
) -> tuple[str, dict]:
    """Blocking Claude CLI call. Returns (reply_text, stats_dict)."""
    full_prompt = _build_prompt(user_text, history)

    cmd = [
        "claude",
        "--model", model,
        "-p", full_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    for d in [x.strip() for x in add_dirs.split(",") if x.strip()]:
        cmd += ["--add-dir", d]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=CLAUDE_WORK_DIR,
        )
        return _parse_cli_output(result.stdout, result.stderr, result.returncode, timeout)
    except subprocess.TimeoutExpired:
        return f"[Timeout] Claude took more than {timeout}s. Try a shorter prompt.", {}
    except FileNotFoundError:
        return "[Error] `claude` CLI not found. Ensure Claude Code is installed and in PATH.", {}


# ─── Claude CLI (async — for Telegram asyncio model) ───────────────────────────

async def ask_claude_async(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_MODEL,
    system: str = CLAUDE_SYSTEM,
    add_dirs: str = CLAUDE_ADD_DIRS,
    perm_mode: str = CLAUDE_PERM_MODE,
    timeout: int = CLAUDE_TIMEOUT,
    on_progress: Optional[callable] = None,
    is_cancelled: Optional[callable] = None,
) -> tuple[str, dict]:
    """Async Claude CLI call with streaming progress. Returns (reply_text, stats_dict).

    on_progress(tool_name: str) is called whenever Claude starts using a tool,
    so the caller can update the user (e.g. "🔧 Reading file…").
    """
    full_prompt = _build_prompt(user_text, history)

    cmd = [
        "claude",
        "--model", model,
        "-p", full_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    for d in [x.strip() for x in add_dirs.split(",") if x.strip()]:
        cmd += ["--add-dir", d]

    # Use 10MB buffer limit — Claude can produce very large JSON lines
    # when writing files (default 64KB is too small)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=CLAUDE_WORK_DIR,
        limit=10 * 1024 * 1024,
    )

    stdout_lines: list[str] = []
    try:
        async def _read_stream():
            while True:
                # Check for cancellation
                if is_cancelled and is_cancelled():
                    proc.kill()
                    return

                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode().strip()
                if not decoded:
                    continue
                stdout_lines.append(decoded)

                # Parse streaming events for progress callbacks
                if on_progress:
                    try:
                        event = json.loads(decoded)
                        etype = event.get("type", "")

                        # Tool use detected in assistant message blocks
                        if etype == "assistant":
                            msg = event.get("message", {})
                            if isinstance(msg, dict):
                                for block in msg.get("content", []):
                                    if block.get("type") == "tool_use":
                                        await on_progress(block.get("name", "tool"))

                        # Tool use detected in content_block_start events
                        elif etype == "content_block_start":
                            cb = event.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                await on_progress(cb.get("name", "tool"))

                    except (json.JSONDecodeError, Exception):
                        pass

        await asyncio.wait_for(_read_stream(), timeout=timeout)
        await proc.wait()
        stderr_data = await proc.stderr.read()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[Timeout] Claude took more than {timeout}s.", {}

    stdout_text = "\n".join(stdout_lines)
    return _parse_cli_output(
        stdout_text, stderr_data.decode(), proc.returncode, timeout
    )


# ─── Claude API (sync — usable from both adapters) ─────────────────────────────

def ask_claude_api(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_API_MODEL,
    system: str = CLAUDE_SYSTEM,
    max_tokens: int = CLAUDE_MAX_TOKENS,
) -> tuple[str, dict]:
    try:
        import anthropic
    except ImportError:
        return "[Error] anthropic package not installed. Run: pip install anthropic", {}

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    messages = history + [{"role": "user", "content": user_text}]
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    text = resp.content[0].text
    stats = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "tools_used": [],
    }
    return text, stats


# ─── Claude Code SDK (async — requires Python 3.10+) ─────────────────────────

async def ask_claude_sdk(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_MODEL,
    system: str = CLAUDE_SYSTEM,
    add_dirs: str = CLAUDE_ADD_DIRS,
    timeout: int = CLAUDE_TIMEOUT,
    on_progress: Optional[callable] = None,
    is_cancelled: Optional[callable] = None,
) -> tuple[str, dict]:
    """Async Claude Code SDK call with streaming progress. Returns (reply_text, stats_dict).

    Uses the claude-code-sdk package which communicates with the Claude CLI
    via subprocess but provides a cleaner Python API with typed messages.
    """
    try:
        from claude_code_sdk import (
            query,
            ClaudeCodeOptions,
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
        )
    except ImportError:
        return "[Error] claude-code-sdk not installed. Run: pip install claude-code-sdk", {}

    full_prompt = _build_prompt(user_text, history)

    # Build options
    opts = ClaudeCodeOptions(
        model=model,
        system_prompt=system,
        cwd=CLAUDE_WORK_DIR,
        permission_mode="bypassPermissions",
        max_turns=50,
    )
    if add_dirs:
        opts.add_dirs = [d.strip() for d in add_dirs.split(",") if d.strip()]

    result_text = ""
    tools_used: list[str] = []
    stats: dict = {}

    try:
        async for message in query(prompt=full_prompt, options=opts):
            # Check for cancellation
            if is_cancelled and is_cancelled():
                break

            if isinstance(message, AssistantMessage):
                # Extract tool use blocks for progress
                for block in getattr(message, "content", []):
                    if isinstance(block, ToolUseBlock):
                        tools_used.append(block.name)
                        if on_progress:
                            try:
                                await on_progress(block.name)
                            except Exception:
                                pass
                    elif isinstance(block, TextBlock):
                        result_text = block.text

            elif isinstance(message, ResultMessage):
                if message.result:
                    result_text = message.result
                usage = message.usage or {}
                stats = {
                    "input_tokens": usage.get("input_tokens", 0)
                                    + usage.get("cache_read_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cost_usd": message.total_cost_usd or 0,
                    "session_id": message.session_id,
                    "num_turns": message.num_turns,
                    "duration_ms": message.duration_ms,
                }

    except asyncio.TimeoutError:
        return f"[Timeout] Claude took more than {timeout}s.", {}
    except Exception as exc:
        return f"[SDK Error] {exc}", {}

    stats["tools_used"] = tools_used
    return result_text or "(no response)", stats


# ─── Internal helpers ───────────────────────────────────────────────────────────

def _build_prompt(user_text: str, history: list[dict]) -> str:
    parts = []
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        parts.append(f"{role}: {msg['content']}")
    parts.append(f"User: {user_text}")
    return "\n\n".join(parts)


def _parse_cli_output(stdout: str, stderr: str, returncode: int, timeout: int) -> tuple[str, dict]:
    output = stdout.strip()
    if returncode != 0 and not output:
        err = stderr.strip()[:500]
        return f"[Claude error] {err}", {}

    result_text = ""
    tools_used: list[str] = []
    stats: dict = {}

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            result_text = result_text or line
            continue

        etype = event.get("type", "")
        if etype == "result":
            result_text = event.get("result", result_text)
            usage = event.get("usage", {})
            stats = {
                "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": event.get("total_cost_usd", 0),
            }
        elif etype == "assistant":
            msg = event.get("message", {})
            if isinstance(msg, dict):
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        result_text = block["text"]
                    elif block.get("type") == "tool_use":
                        tools_used.append(block.get("name", "tool"))
        elif etype == "content_block_start":
            cb = event.get("content_block", {})
            if cb.get("type") == "tool_use":
                tools_used.append(cb.get("name", "tool"))

    stats["tools_used"] = tools_used
    return result_text or output or "(no response)", stats

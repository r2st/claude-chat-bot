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
CLAUDE_MODE       = os.getenv("CLAUDE_MODE", "cli")   # cli | api
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            platform  TEXT NOT NULL,
            user_id   TEXT NOT NULL,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL,
            ts        REAL NOT NULL,
            PRIMARY KEY (platform, user_id, ts)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            platform          TEXT NOT NULL,
            user_id           TEXT NOT NULL,
            message_count     INTEGER DEFAULT 0,
            input_tokens      INTEGER DEFAULT 0,
            output_tokens     INTEGER DEFAULT 0,
            PRIMARY KEY (platform, user_id)
        )
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
        "--system", system,
        "--output-format", "stream-json",
        "--verbose",
        "--print",
        full_prompt,
    ]
    if perm_mode:
        cmd += ["--permission-mode", perm_mode]
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
) -> tuple[str, dict]:
    """Async Claude CLI call. Returns (reply_text, stats_dict)."""
    full_prompt = _build_prompt(user_text, history)

    cmd = [
        "claude",
        "--model", model,
        "--system", system,
        "--output-format", "stream-json",
        "--verbose",
        "--print",
        full_prompt,
    ]
    if perm_mode:
        cmd += ["--permission-mode", perm_mode]
    for d in [x.strip() for x in add_dirs.split(",") if x.strip()]:
        cmd += ["--add-dir", d]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=CLAUDE_WORK_DIR,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[Timeout] Claude took more than {timeout}s.", {}

    return _parse_cli_output(
        stdout.decode(), stderr.decode(), proc.returncode, timeout
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

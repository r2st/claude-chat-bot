"""
Shared Claude invocation + conversation DB + rate limiting.

Used by both telegram_bot.py and whatsapp_bot.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import threading
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

# ─── Connection pool (thread-local SQLite) ──────────────────────────────────────

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection (reused across calls)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
    return _local.conn


# ─── Thread-safe write queue (non-blocking DB writes) ─────────────────────────

import queue as _queue_mod

_write_queue: _queue_mod.Queue | None = None
_writer_thread: threading.Thread | None = None


def _db_writer():
    """Background thread that drains the write queue and batches DB writes."""
    while True:
        ops = []
        try:
            op = _write_queue.get(timeout=1.0)
            ops.append(op)
        except _queue_mod.Empty:
            continue
        # Drain queued items
        while not _write_queue.empty():
            try:
                ops.append(_write_queue.get_nowait())
            except _queue_mod.Empty:
                break
        try:
            conn = _get_conn()
            for sql, params in ops:
                conn.execute(sql, params)
            conn.commit()
        except Exception as e:
            log.error("db_writer batch error: %s", e)


def _ensure_writer():
    """Start the DB writer thread if not already running."""
    global _write_queue, _writer_thread
    if _write_queue is None:
        _write_queue = _queue_mod.Queue(maxsize=1000)
    if _writer_thread is None or not _writer_thread.is_alive():
        _writer_thread = threading.Thread(target=_db_writer, daemon=True)
        _writer_thread.start()


def _enqueue_write(sql: str, params: tuple):
    """Enqueue a non-blocking DB write. Falls back to sync if full."""
    if _write_queue is not None:
        try:
            _write_queue.put_nowait((sql, params))
            return
        except _queue_mod.Full:
            pass
    conn = _get_conn()
    conn.execute(sql, params)
    conn.commit()


# ─── History cache (avoid repeated DB reads for same user) ──────────────────────

_history_cache: dict[str, tuple[float, list[dict]]] = {}
_HISTORY_TTL = 5.0  # seconds


def _cache_key(platform: str, user_id: str) -> str:
    return f"{platform}:{user_id}"


def _invalidate_history(platform: str, user_id: str):
    _history_cache.pop(_cache_key(platform, user_id), None)


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
    _ensure_writer()
    conn = _get_conn()

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

    # Self-improving system tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            rating INTEGER,
            reaction TEXT,
            text_feedback TEXT,
            message_ts REAL,
            response_preview TEXT,
            ts REAL NOT NULL)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            evaluator TEXT NOT NULL,
            score REAL NOT NULL,
            response_preview TEXT,
            metadata TEXT,
            ts REAL NOT NULL)
    """)
    conn.commit()


def load_history(platform: str, user_id: str, limit: int = 20, session_name: str = "") -> list[dict]:
    # Use session-qualified user_id for isolation
    effective_uid = f"{user_id}:{session_name}" if session_name else user_id

    # Check cache first
    key = _cache_key(platform, effective_uid)
    cached = _history_cache.get(key)
    if cached and (time.time() - cached[0]) < _HISTORY_TTL:
        return cached[1]

    conn = _get_conn()
    rows = conn.execute(
        """SELECT role, content FROM conversations
           WHERE platform=? AND user_id=?
           ORDER BY ts DESC LIMIT ?""",
        (platform, effective_uid, limit),
    ).fetchall()
    result = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    _history_cache[key] = (time.time(), result)
    return result


def save_turn(platform: str, user_id: str, user_text: str, reply: str, session_name: str = "") -> None:
    effective_uid = f"{user_id}:{session_name}" if session_name else user_id
    now = time.time()
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO conversations (platform,user_id,role,content,ts) VALUES (?,?,?,?,?)",
        (platform, effective_uid, "user", user_text, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO conversations (platform,user_id,role,content,ts) VALUES (?,?,?,?,?)",
        (platform, effective_uid, "assistant", reply, now + 0.001),
    )
    # Keep last 20 messages per user per platform
    count = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE platform=? AND user_id=?",
        (platform, effective_uid),
    ).fetchone()[0]
    if count > 20:
        conn.execute(
            """DELETE FROM conversations WHERE platform=? AND user_id=? AND ts IN (
               SELECT ts FROM conversations WHERE platform=? AND user_id=?
               ORDER BY ts LIMIT ?)""",
            (platform, effective_uid, platform, effective_uid, count - 20),
        )
    conn.commit()
    _invalidate_history(platform, effective_uid)


def clear_history(platform: str, user_id: str, session_name: str = "") -> None:
    effective_uid = f"{user_id}:{session_name}" if session_name else user_id
    conn = _get_conn()
    conn.execute(
        "DELETE FROM conversations WHERE platform=? AND user_id=?",
        (platform, effective_uid),
    )
    conn.commit()
    _invalidate_history(platform, effective_uid)


def track_usage(platform: str, user_id: str, in_tok: int = 0, out_tok: int = 0) -> None:
    _enqueue_write(
        """INSERT INTO usage (platform, user_id, message_count, input_tokens, output_tokens)
           VALUES (?,?,1,?,?)
           ON CONFLICT(platform,user_id) DO UPDATE SET
               message_count = message_count + 1,
               input_tokens  = input_tokens  + excluded.input_tokens,
               output_tokens = output_tokens + excluded.output_tokens""",
        (platform, user_id, in_tok, out_tok),
    )


def get_usage(platform: str, user_id: str) -> dict:
    conn = _get_conn()
    row = conn.execute(
        "SELECT message_count, input_tokens, output_tokens FROM usage WHERE platform=? AND user_id=?",
        (platform, user_id),
    ).fetchone()
    return {"messages": row[0], "input": row[1], "output": row[2]} if row else {"messages": 0, "input": 0, "output": 0}


def track_tool_usage(platform: str, user_id: str, tools: list[str]) -> None:
    """Record tool usage for analytics (non-blocking)."""
    if not tools:
        return
    now = time.time()
    for tool in tools:
        _enqueue_write(
            "INSERT INTO tool_usage (platform, user_id, tool_name, ts) VALUES (?,?,?,?)",
            (platform, user_id, tool, now),
        )


def track_cost(platform: str, user_id: str, in_tok: int, out_tok: int, cost_usd: float) -> None:
    """Track daily cost for analytics (non-blocking)."""
    from datetime import date
    today = date.today().isoformat()
    _enqueue_write(
        """INSERT INTO cost_tracking (platform, user_id, date, requests, input_tokens, output_tokens, cost_usd)
           VALUES (?,?,?,1,?,?,?)
           ON CONFLICT(platform, user_id, date) DO UPDATE SET
               requests = requests + 1,
               input_tokens = input_tokens + excluded.input_tokens,
               output_tokens = output_tokens + excluded.output_tokens,
               cost_usd = cost_usd + excluded.cost_usd""",
        (platform, user_id, today, in_tok, out_tok, cost_usd),
    )


# ─── Multi-session management ──────────────────────────────────────────────────

_SESSION_TTL = 3600  # 1 hour — CLI sessions stay valid for a while; short TTL caused loss during long tasks


class UserSession:
    """A named conversation session with its own history and Claude session ID."""

    def __init__(self, name: str, platform: str, user_id: str):
        self.name = name
        self.platform = platform
        self.user_id = user_id
        self.claude_session_id: str | None = None
        self.last_active = time.time()
        self.created_at = time.time()
        self.message_count = 0
        self.is_busy = False  # True while a task is running

    @property
    def cli_session_valid(self) -> bool:
        if self.claude_session_id is None:
            return False
        if self.is_busy:
            return True
        return (time.time() - self.last_active) < _SESSION_TTL

    def touch(self):
        self.last_active = time.time()
        self.message_count += 1

    def age_str(self) -> str:
        secs = int(time.time() - self.last_active)
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"

    def status_emoji(self) -> str:
        if self.is_busy:
            return "⚙️"
        if self.cli_session_valid:
            return "🟢"
        return "💤"


class SessionManager:
    """Manages multiple named sessions per user."""

    def __init__(self):
        self._sessions: dict[str, list[UserSession]] = {}  # key → sessions
        self._active: dict[str, int] = {}  # key → active index

    def _key(self, platform: str, user_id: str) -> str:
        return f"{platform}:{user_id}"

    def get_or_create_active(self, platform: str, user_id: str) -> UserSession:
        """Get the active session, or create a default one."""
        key = self._key(platform, user_id)
        sessions = self._sessions.setdefault(key, [])
        if not sessions:
            sessions.append(UserSession("default", platform, user_id))
            self._active[key] = 0
        idx = self._active.get(key, 0)
        return sessions[idx]

    def get_all(self, platform: str, user_id: str) -> list[UserSession]:
        key = self._key(platform, user_id)
        return self._sessions.get(key, [])

    def get_active_index(self, platform: str, user_id: str) -> int:
        key = self._key(platform, user_id)
        return self._active.get(key, 0)

    def create(self, platform: str, user_id: str, name: str) -> UserSession:
        """Create a new session and switch to it."""
        key = self._key(platform, user_id)
        sessions = self._sessions.setdefault(key, [])
        # Limit to 10 sessions per user
        if len(sessions) >= 10:
            # Remove oldest inactive
            oldest = min(
                (s for s in sessions if not s.is_busy),
                key=lambda s: s.last_active,
                default=None,
            )
            if oldest:
                sessions.remove(oldest)
        sess = UserSession(name, platform, user_id)
        sessions.append(sess)
        self._active[key] = len(sessions) - 1
        return sess

    def switch_to(self, platform: str, user_id: str, index: int) -> UserSession | None:
        key = self._key(platform, user_id)
        sessions = self._sessions.get(key, [])
        if 0 <= index < len(sessions):
            self._active[key] = index
            return sessions[index]
        return None

    def delete(self, platform: str, user_id: str, index: int) -> bool:
        key = self._key(platform, user_id)
        sessions = self._sessions.get(key, [])
        if not sessions or index < 0 or index >= len(sessions):
            return False
        if sessions[index].is_busy:
            return False
        sessions.pop(index)
        # Adjust active index
        active = self._active.get(key, 0)
        if active >= len(sessions):
            self._active[key] = max(0, len(sessions) - 1)
        elif active > index:
            self._active[key] = active - 1
        # Ensure at least one session exists
        if not sessions:
            sessions.append(UserSession("default", platform, user_id))
            self._active[key] = 0
        return True

    def clear_active(self, platform: str, user_id: str):
        """Clear the active session's history and CLI session."""
        sess = self.get_or_create_active(platform, user_id)
        sess.claude_session_id = None
        sess.message_count = 0


_session_mgr = SessionManager()


# Legacy API compatibility
def get_session_id(platform: str, user_id: str) -> str | None:
    sess = _session_mgr.get_or_create_active(platform, user_id)
    return sess.claude_session_id if sess.cli_session_valid else None


def set_session_id(platform: str, user_id: str, session_id: str):
    sess = _session_mgr.get_or_create_active(platform, user_id)
    sess.claude_session_id = session_id
    sess.touch()


def clear_session(platform: str, user_id: str):
    _session_mgr.clear_active(platform, user_id)
    _invalidate_history(platform, user_id)


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
    on_text: Optional[callable] = None,
    is_cancelled: Optional[callable] = None,
    platform: str = "",
    user_id: str = "",
    resume_session_id: str = "",
) -> tuple[str, dict]:
    """Async Claude CLI call with streaming progress. Returns (reply_text, stats_dict).

    on_progress(tool_name: str) is called whenever Claude starts using a tool.
    on_text(partial_text: str) is called when text content is received.
    """
    # Try to resume existing session (skips project re-indexing)
    session_id = resume_session_id or (get_session_id(platform, user_id) if platform else None)

    full_prompt = _build_prompt(user_text, history) if history else user_text

    if session_id:
        # Resume: send only new message (CLI has full conversation context)
        cmd = [
            "claude",
            "--model", model,
            "-p", user_text,
            "--resume", session_id,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
    else:
        # New session: include conversation history in prompt
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
                if on_progress or on_text:
                    try:
                        event = json.loads(decoded)
                        etype = event.get("type", "")

                        # Tool use detected in assistant message blocks
                        if etype == "assistant":
                            msg = event.get("message", {})
                            if isinstance(msg, dict):
                                for block in msg.get("content", []):
                                    if block.get("type") == "tool_use" and on_progress:
                                        detail = _extract_tool_detail(block)
                                        await on_progress(block.get("name", "tool"), detail)
                                    elif block.get("type") == "text" and on_text:
                                        await on_text(block.get("text", ""))

                        # Tool use detected in content_block_start events
                        elif etype == "content_block_start":
                            cb = event.get("content_block", {})
                            if cb.get("type") == "tool_use" and on_progress:
                                detail = _extract_tool_detail(cb)
                                await on_progress(cb.get("name", "tool"), detail)

                        # Partial text in content_block_delta
                        elif etype == "content_block_delta" and on_text:
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                await on_text(delta.get("text", ""))

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
    result = _parse_cli_output(
        stdout_text, stderr_data.decode(), proc.returncode, timeout
    )

    # If resume failed (error or empty), retry with full history as a new session
    if session_id and (proc.returncode != 0 or not result[0] or result[0].startswith("[Claude error]")):
        log.warning("Session resume failed (rc=%d), retrying with full history", proc.returncode)
        # Invalidate the stale session
        if platform:
            active_sess = _session_mgr.get_or_create_active(platform, user_id)
            active_sess.claude_session_id = None

        retry_cmd = [
            "claude",
            "--model", model,
            "-p", full_prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        for d in [x.strip() for x in add_dirs.split(",") if x.strip()]:
            retry_cmd += ["--add-dir", d]

        proc2 = await asyncio.create_subprocess_exec(
            *retry_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CLAUDE_WORK_DIR,
            limit=10 * 1024 * 1024,
        )
        retry_lines: list[str] = []
        try:
            async def _read_retry():
                while True:
                    if is_cancelled and is_cancelled():
                        proc2.kill()
                        return
                    line = await proc2.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    if decoded:
                        retry_lines.append(decoded)
                        if on_progress or on_text:
                            try:
                                event = json.loads(decoded)
                                etype = event.get("type", "")
                                if etype == "content_block_delta" and on_text:
                                    delta = event.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        await on_text(delta.get("text", ""))
                            except Exception:
                                pass

            await asyncio.wait_for(_read_retry(), timeout=timeout)
            await proc2.wait()
            stderr2 = await proc2.stderr.read()
        except asyncio.TimeoutError:
            proc2.kill()
            await proc2.wait()
            return f"[Timeout] Claude took more than {timeout}s.", {}

        result = _parse_cli_output(
            "\n".join(retry_lines), stderr2.decode(), proc2.returncode, timeout
        )

    return result


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
    on_text: Optional[callable] = None,
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
                                inp = getattr(block, "input", {}) or {}
                                detail = _extract_tool_detail({"input": inp})
                                await on_progress(block.name, detail)
                            except Exception:
                                pass
                    elif isinstance(block, TextBlock):
                        result_text = block.text
                        if on_text:
                            try:
                                await on_text(block.text)
                            except Exception:
                                pass

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

def _extract_tool_detail(block: dict) -> str:
    """Extract a short detail string from a tool_use block (e.g. file path, command)."""
    inp = block.get("input", {})
    if not isinstance(inp, dict):
        return ""
    # Read/Write/Edit → file_path
    fp = inp.get("file_path", "")
    if fp:
        # Show just the filename or last 2 path components
        parts = fp.rsplit("/", 2)
        return "/".join(parts[-2:]) if len(parts) > 2 else fp
    # Bash → command (truncated)
    cmd = inp.get("command", "")
    if cmd:
        return cmd[:50]
    # Grep → pattern
    pattern = inp.get("pattern", "")
    if pattern:
        return f"/{pattern[:30]}/"
    return ""


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
                "session_id": event.get("session_id", ""),
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

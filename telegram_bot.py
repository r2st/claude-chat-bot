"""
Telegram adapter — wraps claude_core for the Telegram Bot API.
Run via main.py with BOT_MODE=telegram or BOT_MODE=both.
"""

import asyncio
import itertools
import logging
import os
import re
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import claude_core as cc

log = logging.getLogger(__name__)

PLATFORM = "telegram"

# ─── Telegram-specific config ───────────────────────────────────────────────────

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ALLOWED_USER_IDS: set[int] = set()
_raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
if _raw:
    ALLOWED_USER_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

# Per-user overrides (in-memory)
_user_model:   dict[int, str] = {}
_user_perm:    dict[int, str] = {}
_user_verbose: dict[int, int] = {}

PERMISSION_MODES = {
    "default":           "Default (prompts for everything)",
    "acceptEdits":       "Accept Edits (auto-approve file r/w)",
    "auto":              "Auto (approve most actions)",
    "bypassPermissions": "Bypass All (no restrictions)",
}
CLI_MODELS = {
    "haiku":  "Haiku (fastest)",
    "sonnet": "Sonnet (balanced)",
    "opus":   "Opus (most capable)",
}
VERBOSE_LEVELS = {0: "Quiet", 1: "Normal", 2: "Detailed"}

_DEFAULT_MODEL = os.getenv("CLAUDE_CLI_MODEL", cc.CLAUDE_MODEL)
_DEFAULT_PERM  = os.getenv("CLAUDE_CLI_PERMISSION_MODE", cc.CLAUDE_PERM_MODE)


# ─── Per-user getters ───────────────────────────────────────────────────────────

def _model(uid: int)   -> str: return _user_model.get(uid, _DEFAULT_MODEL)
def _perm(uid: int)    -> str: return _user_perm.get(uid, _DEFAULT_PERM)
def _verbose(uid: int) -> int: return _user_verbose.get(uid, 1)


# ─── Auth / rate limit ──────────────────────────────────────────────────────────

def _allowed(uid: int) -> bool:
    return not ALLOWED_USER_IDS or uid in ALLOWED_USER_IDS


# ─── Typing indicator ───────────────────────────────────────────────────────────

async def _typing_loop(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            continue


# ─── Message deduplication (Telegram can retry sending the same update) ────────

_processed_msgs: dict[int, float] = {}  # message_id → timestamp
_DEDUP_TTL = 30  # seconds


def _is_duplicate(msg_id: int) -> bool:
    """Return True if this message was already processed recently."""
    now = time.time()
    # Clean old entries
    stale = [k for k, v in _processed_msgs.items() if now - v > _DEDUP_TTL]
    for k in stale:
        del _processed_msgs[k]
    if msg_id in _processed_msgs:
        return True
    _processed_msgs[msg_id] = now
    return False


# ─── Task manager for concurrent sessions ─────────────────────────────────────

_task_counter = itertools.count(1)

# Max concurrent tasks per user
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))


class TaskSession:
    """Manages a single running Claude task with progress, cancel, and identity."""

    def __init__(self, placeholder, uid: int, prompt_preview: str):
        self.task_id = next(_task_counter)
        self.placeholder = placeholder
        self.chat_id = None  # set externally for intermediate messages
        self.bot = None       # set externally
        self.uid = uid
        self.prompt_preview = prompt_preview[:40]
        self.start_time = time.time()
        self.tools: list[str] = []
        self.tool_count = 0
        self._last_update = 0.0
        self._last_status = ""
        self._cancelled = False
        self._heartbeat_task: asyncio.Task | None = None
        self._partial_text = ""
        self._phase = "thinking"  # thinking → working → streaming
        self._current_activity = ""  # detailed activity description
        self._sent_intermediate = False  # only send one intermediate update

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self):
        self._cancelled = True

    def _elapsed(self) -> str:
        secs = int(time.time() - self.start_time)
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"

    def _progress_bar(self) -> str:
        """Visual progress bar based on activity."""
        secs = int(time.time() - self.start_time)
        # Animated fill that moves based on time + activity
        if self._phase == "streaming":
            fill = min(9, 7 + (secs % 3))
        elif self.tool_count > 0:
            fill = min(7, 2 + self.tool_count)
        else:
            fill = min(3, 1 + secs // 10)
        bar = "▓" * fill + "░" * (10 - fill)
        return f"[{bar}]"

    def _build_status(self) -> str:
        elapsed = self._elapsed()

        # Show task ID if user has multiple active tasks
        user_tasks = _task_registry.get_user_tasks(self.uid)
        task_label = f"  `#{self.task_id}`" if len(user_tasks) > 1 else ""

        # Phase with emoji
        if self._phase == "thinking":
            phase = "🧠 *Thinking…*"
        elif self._phase == "working":
            phase = "⚙️ *Working…*"
        else:
            phase = "✍️ *Writing…*"

        progress = self._progress_bar()
        lines = [f"{phase} `{elapsed}`{task_label}", progress]

        # Current activity (detailed)
        if self._current_activity:
            lines.append(f"\n{self._current_activity}")
        elif self.tools:
            last_tool = self.tools[-1]
            lines.append(f"\n{_tool_label(last_tool)}")

        # Tool history summary
        if self.tool_count > 1:
            unique = list(dict.fromkeys(self.tools))
            summary_parts = []
            for t in unique[-4:]:
                count = self.tools.count(t)
                icon = _TOOL_ICONS.get(t, "🔧")
                summary_parts.append(f"{icon}×{count}" if count > 1 else icon)
            lines.append(f"\n{'  '.join(summary_parts)}  _({self.tool_count} steps)_")

        # Show partial streaming text preview
        if self._partial_text:
            preview = self._partial_text[-400:]
            if len(self._partial_text) > 400:
                preview = "…" + preview
            # Escape markdown special chars in preview to avoid parse errors
            lines.append(f"\n{'─' * 20}\n{preview}")

        return "\n".join(lines)

    async def on_tool(self, tool_name: str, detail: str = ""):
        """Called when Claude starts using a tool."""
        self._phase = "working"
        self.tools.append(tool_name)
        self.tool_count += 1
        if detail:
            self._current_activity = f"{_tool_label(tool_name).rstrip('…')} `{detail}`…"
        else:
            self._current_activity = _tool_label(tool_name)
        await self._update()

    async def on_text(self, text: str):
        """Called when Claude streams text content."""
        self._phase = "streaming"
        self._current_activity = ""
        # Handle both full-text updates and delta appends
        if len(text) > len(self._partial_text):
            self._partial_text = text
        else:
            self._partial_text += text
        await self._update()

    async def _update(self, force: bool = False):
        """Update the placeholder message (rate-limited to avoid Telegram 429s)."""
        now = time.time()
        # Adaptive rate: faster for first 15s (2s), slower after (4s)
        min_interval = 2.0 if (now - self.start_time) < 15 else 4.0
        if not force and (now - self._last_update < min_interval):
            return
        self._last_update = now

        status = self._build_status()
        # Don't send identical updates (Telegram would error "message not modified")
        if status == self._last_status:
            return
        self._last_status = status

        # Truncate to stay within Telegram's 4096 char limit
        if len(status) > 4000:
            status = status[:4000] + "\n…"
        cancel_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ Cancel", callback_data=f"tg:cancel:{self.task_id}")]
        ])
        try:
            await self.placeholder.edit_text(
                status,
                reply_markup=cancel_btn,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            # Fallback without markdown if it fails
            try:
                await self.placeholder.edit_text(
                    status,
                    reply_markup=cancel_btn,
                )
            except Exception:
                pass

    async def _heartbeat(self):
        """Periodic updates to keep elapsed time fresh."""
        await asyncio.sleep(4)
        while not self._cancelled:
            await self._update(force=True)
            elapsed = time.time() - self.start_time

            # For very long tasks (>60s), send intermediate result as new message
            if (elapsed > 60 and not self._sent_intermediate
                    and self._partial_text and len(self._partial_text) > 100
                    and self.bot and self.chat_id):
                self._sent_intermediate = True
                preview = self._partial_text[:2000]
                if len(self._partial_text) > 2000:
                    preview += "\n\n⏳ _Still working… full response coming soon._"
                try:
                    await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=f"📝 *Interim update* ({self._elapsed()}):\n\n{preview}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    try:
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"📝 Interim update ({self._elapsed()}):\n\n{preview}",
                        )
                    except Exception:
                        pass

            if elapsed < 30:
                interval = 4
            elif elapsed < 120:
                interval = 8
            else:
                interval = 12
            await asyncio.sleep(interval)

    def start_heartbeat(self):
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def stop(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    def finish_summary(self) -> str:
        elapsed = self._elapsed()
        if self.tool_count > 0:
            return f"✅ _{self.tool_count} tools · {elapsed}_"
        return f"✅ _{elapsed}_"


class TaskRegistry:
    """Thread-safe registry of active tasks across all users."""

    def __init__(self):
        self._tasks: dict[int, TaskSession] = {}  # task_id → TaskSession

    def register(self, task: TaskSession) -> None:
        self._tasks[task.task_id] = task

    def unregister(self, task_id: int) -> None:
        self._tasks.pop(task_id, None)

    def get(self, task_id: int) -> TaskSession | None:
        return self._tasks.get(task_id)

    def get_user_tasks(self, uid: int) -> list[TaskSession]:
        return [t for t in self._tasks.values() if t.uid == uid]

    def user_task_count(self, uid: int) -> int:
        return sum(1 for t in self._tasks.values() if t.uid == uid)

    def cancel_all_user(self, uid: int) -> int:
        """Cancel all tasks for a user. Returns count cancelled."""
        count = 0
        for t in self.get_user_tasks(uid):
            t.cancel()
            count += 1
        return count


_task_registry = TaskRegistry()

# ─── Response store (for pagination and retry) ─────────────────────────────────

_response_store: dict[str, dict] = {}  # response_id → {text, uid, prompt, page}
_response_counter = itertools.count(1)

RESPONSE_PAGE_SIZE = 3000  # chars per page


def _store_response(uid: int, prompt: str, text: str) -> str:
    """Store a long response and return its ID for pagination."""
    rid = f"r{next(_response_counter)}"
    _response_store[rid] = {"text": text, "uid": uid, "prompt": prompt}
    # Keep only last 50 responses in memory
    if len(_response_store) > 50:
        oldest = list(_response_store.keys())[0]
        del _response_store[oldest]
    return rid


# ─── Reply helper ───────────────────────────────────────────────────────────────

# Regex to find bare URLs not already inside markdown link syntax
_URL_RE = re.compile(r'(?<!\()(https?://[^\s\)\]>]+)')
# Regex to detect existing markdown links [text](url)
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')


def _protect_urls_for_markdown(text: str) -> str:
    """Wrap bare URLs in markdown link format so Telegram renders them clickable.

    Bare URLs containing underscores/special chars break Telegram's legacy Markdown
    parser. Wrapping them as [url](url) ensures they display as clickable links.
    """
    # First, collect positions of existing markdown links to avoid double-wrapping
    existing_link_spans = set()
    for m in _MD_LINK_RE.finditer(text):
        existing_link_spans.add((m.start(), m.end()))

    def _replace_bare_url(match):
        url = match.group(1)
        start = match.start(1)
        # Check if this URL is already inside a markdown link
        for ls, le in existing_link_spans:
            if ls <= start < le:
                return match.group(0)
        # Strip trailing markdown punctuation that got captured with the URL
        trailing = ""
        while url and url[-1] in ("*", "_", "`", "~"):
            trailing = url[-1] + trailing
            url = url[:-1]
        # Wrap bare URL as clickable markdown link
        return f"[{url}]({url}){trailing}"

    return _URL_RE.sub(_replace_bare_url, text)


async def _send(placeholder, update: Update, text: str):
    chunk = 4096
    if not text or not text.strip():
        await placeholder.edit_text("_(empty response)_", parse_mode=ParseMode.MARKDOWN, reply_markup=None)
        return
    # Protect URLs so they remain clickable in Markdown mode
    md_text = _protect_urls_for_markdown(text)
    try:
        if len(md_text) <= chunk:
            await placeholder.edit_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
        else:
            await placeholder.edit_text(md_text[:chunk], parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            for i in range(chunk, len(md_text), chunk):
                await update.effective_message.reply_text(md_text[i:i+chunk], parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # Fallback 1: plain text edit
        try:
            if len(text) <= chunk:
                await placeholder.edit_text(text, reply_markup=None)
            else:
                await placeholder.edit_text(text[:chunk], reply_markup=None)
                for i in range(chunk, len(text), chunk):
                    await update.effective_message.reply_text(text[i:i+chunk])
        except Exception:
            # Fallback 2: send as new message if editing fails entirely
            try:
                await placeholder.edit_text("✅ Done — see reply below.", reply_markup=None)
            except Exception:
                pass
            try:
                if len(text) <= chunk:
                    await update.effective_message.reply_text(text)
                else:
                    for i in range(0, len(text), chunk):
                        await update.effective_message.reply_text(text[i:i+chunk])
            except Exception as exc:
                log.error("_send all fallbacks failed: %s", exc)


# ─── Tool name → friendly emoji/label ──────────────────────────────────────────

_TOOL_LABELS = {
    "Bash":       "🖥️ Running command…",
    "Read":       "📖 Reading file…",
    "Write":      "📝 Writing file…",
    "Edit":       "✏️ Editing file…",
    "Grep":       "🔍 Searching code…",
    "ListDir":    "📂 Listing directory…",
    "WebSearch":  "🌐 Searching the web…",
    "WebFetch":   "🌐 Fetching page…",
    "Agent":      "🤖 Delegating to agent…",
    "TodoWrite":  "📋 Planning…",
    "TodoRead":   "📋 Checking plan…",
}

_TOOL_ICONS = {
    "Bash": "🖥️", "Read": "📖", "Write": "📝", "Edit": "✏️",
    "Grep": "🔍", "ListDir": "📂", "WebSearch": "🌐", "WebFetch": "🌐",
    "Agent": "🤖", "TodoWrite": "📋", "TodoRead": "📋",
}


def _tool_label(tool_name: str) -> str:
    return _TOOL_LABELS.get(tool_name, f"🔧 Using {tool_name}…")


# ─── Core ask ───────────────────────────────────────────────────────────────────

# Per-user engine override (cli, sdk, api)
_user_engine: dict[int, str] = {}

ENGINE_MODES = {
    "cli": "CLI (default — claude subprocess)",
    "sdk": "SDK (claude-code-sdk, streaming)",
    "api": "API (Anthropic Messages API)",
}


def _engine(uid: int) -> str:
    return _user_engine.get(uid, cc.CLAUDE_MODE)


def _active_session(uid: int) -> "cc.UserSession":
    return cc._session_mgr.get_or_create_active(PLATFORM, str(uid))


async def _ask(uid: int, text: str, tracker: TaskSession | None = None, session: "cc.UserSession | None" = None) -> tuple[str, dict]:
    sess = session or cc._session_mgr.get_or_create_active(PLATFORM, str(uid))
    history = cc.load_history(PLATFORM, str(uid), session_name=sess.name)
    engine = _engine(uid)

    if engine == "api":
        return cc.ask_claude_api(text, history, system=cc.CLAUDE_SYSTEM)

    # Progress callback via tracker
    async def _on_progress(tool_name: str, detail: str = ""):
        if tracker:
            await tracker.on_tool(tool_name, detail)

    # Text streaming callback
    async def _on_text(text_chunk: str):
        if tracker:
            await tracker.on_text(text_chunk)

    # Cancellation check
    def _is_cancelled() -> bool:
        return tracker.cancelled if tracker else False

    if engine == "sdk":
        result = await cc.ask_claude_sdk(
            text, history,
            model=_model(uid),
            system=cc.CLAUDE_SYSTEM,
            add_dirs=cc.CLAUDE_ADD_DIRS,
            timeout=cc.CLAUDE_TIMEOUT,
            on_progress=_on_progress,
            on_text=_on_text,
            is_cancelled=_is_cancelled,
        )
        if result[1].get("session_id"):
            sess.claude_session_id = result[1]["session_id"]
            sess.touch()
        return result

    # Default: CLI mode
    cli_sid = sess.claude_session_id if sess.cli_session_valid else ""
    result = await cc.ask_claude_async(
        text, history,
        model=_model(uid),
        system=cc.CLAUDE_SYSTEM,
        add_dirs=cc.CLAUDE_ADD_DIRS,
        perm_mode=_perm(uid),
        timeout=cc.CLAUDE_TIMEOUT,
        on_progress=_on_progress,
        on_text=_on_text,
        is_cancelled=_is_cancelled,
        platform=PLATFORM,
        user_id=str(uid),
        resume_session_id=cli_sid or "",
    )
    if result[1].get("session_id"):
        sess.claude_session_id = result[1]["session_id"]
        sess.touch()
    return result


# ─── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm Claude on Telegram.\n\n"
        "Send me any message and I'll work on it. You can send multiple messages — they run as parallel tasks.\n\n"
        "Commands:\n"
        "/tasks — show active running tasks\n"
        "/cancel — cancel a task (or all)\n"
        "/sessions — view/switch sessions\n"
        "/new <name> — create a new session\n"
        "/switch — switch to another session\n"
        "/browse — browse project folders interactively\n"
        "/reset — clear conversation history\n"
        "/model — switch Claude model\n"
        "/engine — switch engine (cli/sdk/api)\n"
        "/permissions — change CLI permission mode\n"
        "/verbose — set verbosity (0/1/2)\n"
        "/usage — show usage stats\n"
        "/mode — show current settings\n"
        "/id — show your Telegram user ID"
    )


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    sess = _active_session(int(uid))
    cc.clear_history(PLATFORM, uid, session_name=sess.name)
    cc.clear_session(PLATFORM, uid)
    sess.claude_session_id = None
    sess.message_count = 0
    await update.message.reply_text(f"History cleared for session `{sess.name}`.", parse_mode="Markdown")


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sess = _active_session(uid)
    info = (
        f"Platform: `telegram`\n"
        f"Session: `{sess.name}` {sess.status_emoji()}\n"
        f"Engine: `{_engine(uid)}`\n"
        f"Model: `{_model(uid)}`\n"
        f"Permission mode: `{_perm(uid) or 'default'}`\n"
        f"Verbose: `{_verbose(uid)}`\n"
        f"Timeout: `{cc.CLAUDE_TIMEOUT}s`"
    )
    await update.message.reply_text(info, parse_mode="Markdown")


async def cmd_engine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Switch between CLI, SDK, and API execution engines."""
    uid = update.effective_user.id
    cur = _engine(uid)
    btns = [
        [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:engine:{k}")]
        for k, lbl in ENGINE_MODES.items()
    ]
    await update.message.reply_text(
        f"Current engine: *{cur}*\n\nSelect execution engine:",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show active tasks for this user."""
    uid = update.effective_user.id
    tasks = _task_registry.get_user_tasks(uid)

    if not tasks:
        await update.message.reply_text("No active tasks.")
        return

    lines = [f"*Active tasks ({len(tasks)}):*\n"]
    for t in tasks:
        lines.append(f"  `#{t.task_id}` — {t.prompt_preview}… ({t._elapsed()})")

    btns = []
    for t in tasks:
        btns.append([InlineKeyboardButton(
            f"⏹ Cancel #{t.task_id}", callback_data=f"tg:cancel:{t.task_id}"
        )])
    btns.append([InlineKeyboardButton("⏹ Cancel All", callback_data=f"tg:cancelall:{uid}")])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel a specific task or all tasks. Usage: /cancel [task_id|all]"""
    uid = update.effective_user.id
    args = ctx.args

    if args and args[0].lower() == "all":
        count = _task_registry.cancel_all_user(uid)
        await update.message.reply_text(f"⏹ Cancelled {count} task(s).")
        return

    if args:
        try:
            task_id = int(args[0].lstrip("#"))
            task = _task_registry.get(task_id)
            if task and task.uid == uid:
                task.cancel()
                await update.message.reply_text(f"⏹ Cancelling task `#{task_id}`.", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"Task `#{task_id}` not found.", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Usage: `/cancel <task_id>` or `/cancel all`", parse_mode="Markdown")
        return

    # No args — show active tasks with cancel buttons
    tasks = _task_registry.get_user_tasks(uid)
    if not tasks:
        await update.message.reply_text("No active tasks to cancel.")
        return

    btns = []
    for t in tasks:
        btns.append([InlineKeyboardButton(
            f"⏹ #{t.task_id} — {t.prompt_preview[:20]}…", callback_data=f"tg:cancel:{t.task_id}"
        )])
    btns.append([InlineKeyboardButton("⏹ Cancel All", callback_data=f"tg:cancelall:{uid}")])
    await update.message.reply_text(
        f"*{len(tasks)} active task(s).* Which to cancel?",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── Session management ────────────────────────────────────────────────────────

async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all sessions for this user with status indicators."""
    uid = update.effective_user.id
    sessions = cc._session_mgr.get_all(PLATFORM, str(uid))
    if not sessions:
        sessions = [cc._session_mgr.get_or_create_active(PLATFORM, str(uid))]

    active_idx = cc._session_mgr.get_active_index(PLATFORM, str(uid))

    lines = ["*Your sessions:*\n"]
    btns = []
    for i, s in enumerate(sessions):
        marker = " ← active" if i == active_idx else ""
        lines.append(
            f"{s.status_emoji()} `{s.name}` — {s.message_count} msgs, {s.age_str()}{marker}"
        )
        if i != active_idx:
            btns.append([InlineKeyboardButton(
                f"Switch to: {s.name}", callback_data=f"tg:sess:sw:{i}"
            )])

    btns.append([InlineKeyboardButton("➕ New session", callback_data="tg:sess:new:_")])
    if len(sessions) > 1:
        btns.append([InlineKeyboardButton("🗑 Delete a session…", callback_data="tg:sess:delmenu:_")])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Create a new named session. Usage: /new <name>"""
    uid = update.effective_user.id
    name = " ".join(ctx.args).strip() if ctx.args else f"session-{int(time.time()) % 10000}"
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", name)[:20]

    sess = cc._session_mgr.create(PLATFORM, str(uid), name)
    await update.message.reply_text(
        f"✅ Created and switched to session `{sess.name}`",
        parse_mode="Markdown",
    )


async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Switch to another session by name or index."""
    uid = update.effective_user.id
    sessions = cc._session_mgr.get_all(PLATFORM, str(uid))

    if not sessions or len(sessions) <= 1:
        await update.message.reply_text("Only one session exists. Use /new to create more.")
        return

    if ctx.args:
        target = ctx.args[0].strip()
        for i, s in enumerate(sessions):
            if s.name == target or str(i) == target:
                cc._session_mgr.switch_to(PLATFORM, str(uid), i)
                await update.message.reply_text(
                    f"✅ Switched to `{s.name}`", parse_mode="Markdown"
                )
                return
        await update.message.reply_text(f"Session `{target}` not found.", parse_mode="Markdown")
        return

    active_idx = cc._session_mgr.get_active_index(PLATFORM, str(uid))
    btns = []
    for i, s in enumerate(sessions):
        if i == active_idx:
            continue
        btns.append([InlineKeyboardButton(
            f"{s.status_emoji()} {s.name} ({s.message_count} msgs)",
            callback_data=f"tg:sess:sw:{i}",
        )])

    await update.message.reply_text(
        "Switch to which session?",
        reply_markup=InlineKeyboardMarkup(btns),
    )


# ─── Folder browser ─────────────────────────────────────────────────────────────

BROWSE_ROOT = Path(cc.CLAUDE_WORK_DIR)
BROWSE_PAGE_SIZE = 8

# Path registry: maps short IDs to absolute paths (per-session, in-memory)
_path_registry: dict[str, Path] = {}
_path_counter = itertools.count(1)


def _pid(path: Path) -> str:
    """Register a path and return a short ID for use in callback_data."""
    for k, v in _path_registry.items():
        if v == path:
            return k
    pid = f"p{next(_path_counter)}"
    _path_registry[pid] = path
    return pid


def _resolve_pid(pid: str) -> Path | None:
    """Look up a path by its short ID."""
    return _path_registry.get(pid)


def _browse_buttons(directory: Path, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Build message text and inline buttons for a directory listing."""
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        parent_id = _pid(directory.parent)
        return "⛔ Permission denied.", InlineKeyboardMarkup([
            [InlineKeyboardButton("⬆️ ..", callback_data=f"tg:br:{parent_id}:0")]
        ])

    dirs = [e for e in entries if e.is_dir() and not e.name.startswith(".")]
    files = [e for e in entries if e.is_file() and not e.name.startswith(".")]
    all_items = dirs + files

    total_pages = max(1, (len(all_items) + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
    page = min(page, total_pages - 1)
    start = page * BROWSE_PAGE_SIZE
    page_items = all_items[start:start + BROWSE_PAGE_SIZE]

    rel = directory.relative_to(BROWSE_ROOT) if directory != BROWSE_ROOT else Path(".")
    header = f"📂 `{rel}/`\n_{len(dirs)} folders, {len(files)} files_"
    if total_pages > 1:
        header += f" — page {page + 1}/{total_pages}"

    dir_id = _pid(directory)
    btns = []
    for item in page_items:
        item_id = _pid(item)
        if item.is_dir():
            label = f"📁 {item.name}/"
            cb = f"tg:br:{item_id}:0"
        else:
            size = item.stat().st_size
            if size < 1024:
                sz = f"{size}B"
            elif size < 1024 * 1024:
                sz = f"{size // 1024}KB"
            else:
                sz = f"{size // (1024*1024)}MB"
            label = f"📄 {item.name} ({sz})"
            cb = f"tg:bf:{item_id}"
        btns.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if directory != BROWSE_ROOT:
        parent_id = _pid(directory.parent)
        nav.append(InlineKeyboardButton("⬆️ ..", callback_data=f"tg:br:{parent_id}:0"))
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tg:br:{dir_id}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tg:br:{dir_id}:{page+1}"))
    if nav:
        btns.append(nav)

    btns.append([InlineKeyboardButton(
        "🤖 Ask Claude about this folder",
        callback_data=f"tg:ba:{dir_id}"
    )])

    return header, InlineKeyboardMarkup(btns)


async def cmd_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Browse project folders interactively."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return

    args_text = " ".join(ctx.args) if ctx.args else ""
    if args_text:
        target = BROWSE_ROOT / args_text
        if not target.exists() or not target.is_dir():
            await update.message.reply_text(f"Directory not found: `{args_text}`", parse_mode="Markdown")
            return
    else:
        target = BROWSE_ROOT

    header, markup = _browse_buttons(target)
    await update.message.reply_text(header, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)


async def _handle_browse_callback(q, uid: int):
    """Handle br/bf/bv/ba callbacks for folder browsing."""
    data = q.data
    parts = data.split(":")
    if len(parts) < 3:
        return
    kind = parts[1]

    if kind == "br":
        # tg:br:<path_id>:<page>
        if len(parts) < 4:
            return
        pid, page = parts[2], int(parts[3])
        dir_path = _resolve_pid(pid)
        if not dir_path or not dir_path.is_dir():
            await q.edit_message_text("Directory no longer exists.")
            return
        try:
            dir_path.resolve().relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await q.edit_message_text("⛔ Access denied.")
            return

        header, markup = _browse_buttons(dir_path, page)
        await q.edit_message_text(header, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

    elif kind == "bf":
        # tg:bf:<path_id>
        pid = parts[2]
        file_path = _resolve_pid(pid)
        if not file_path or not file_path.is_file():
            await q.edit_message_text("File no longer exists.")
            return
        try:
            file_path.resolve().relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await q.edit_message_text("⛔ Access denied.")
            return

        stat = file_path.stat()
        rel = file_path.relative_to(BROWSE_ROOT)
        info = f"📄 `{rel}`\nSize: {stat.st_size:,} bytes"

        parent_id = _pid(file_path.parent)
        btns = [
            [InlineKeyboardButton("📖 View (first 50 lines)", callback_data=f"tg:bv:{pid}")],
            [InlineKeyboardButton("🤖 Ask Claude about file", callback_data=f"tg:ba:{pid}")],
            [InlineKeyboardButton("⬆️ Back", callback_data=f"tg:br:{parent_id}:0")],
        ]
        await q.edit_message_text(info, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.MARKDOWN)

    elif kind == "bv":
        # tg:bv:<path_id>
        pid = parts[2]
        file_path = _resolve_pid(pid)
        if not file_path or not file_path.is_file():
            await q.edit_message_text("File no longer exists.")
            return
        try:
            file_path.resolve().relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await q.edit_message_text("⛔ Access denied.")
            return

        try:
            lines = file_path.read_text(errors="replace").splitlines()[:50]
            content = "\n".join(lines)
            if len(content) > 3500:
                content = content[:3500] + "\n…(truncated)"
            rel = file_path.relative_to(BROWSE_ROOT)
            text = f"📄 `{rel}` (first 50 lines):\n```\n{content}\n```"
        except Exception as e:
            text = f"Error reading file: {e}"

        parent_id = _pid(file_path.parent)
        btns = [
            [InlineKeyboardButton("🤖 Ask Claude about file", callback_data=f"tg:ba:{pid}")],
            [InlineKeyboardButton("⬆️ Back", callback_data=f"tg:br:{parent_id}:0")],
        ]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.MARKDOWN)

    elif kind == "ba":
        # tg:ba:<path_id>
        pid = parts[2]
        target = _resolve_pid(pid)
        if not target or not target.exists():
            await q.edit_message_text("Path no longer exists.")
            return
        try:
            target.resolve().relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await q.edit_message_text("⛔ Access denied.")
            return

        rel = target.relative_to(BROWSE_ROOT)
        if target.is_dir():
            prompt = f"List and briefly describe the contents of the directory: {rel}/"
        else:
            prompt = f"Read and summarize this file: {rel}"

        await q.edit_message_text(f"🤖 Asking Claude about `{rel}`…", reply_markup=None, parse_mode=ParseMode.MARKDOWN)

        task = TaskSession(q.message, uid, f"[Browse] {rel}")
        task.start_heartbeat()
        _task_registry.register(task)
        try:
            reply, stats = await _ask(uid, prompt, tracker=task)
            browse_sess = _active_session(uid)
            cc.save_turn(PLATFORM, str(uid), prompt, reply, session_name=browse_sess.name)
            cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))

            back_dir = target if target.is_dir() else target.parent
            back_id = _pid(back_dir)
            btns = [[InlineKeyboardButton("⬆️ Back to browser", callback_data=f"tg:br:{back_id}:0")]]
            md_text = _protect_urls_for_markdown(reply)
            if len(md_text) > 4000:
                md_text = md_text[:4000] + "\n…(truncated)"
            try:
                await q.message.edit_text(md_text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await q.message.edit_text(reply[:4000], reply_markup=InlineKeyboardMarkup(btns))
        except Exception as exc:
            await q.message.edit_text(f"Error: {exc}", reply_markup=None)
        finally:
            await task.stop()
            _task_registry.unregister(task.task_id)


async def cmd_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = cc.get_usage(PLATFORM, str(uid))
    await update.message.reply_text(
        f"*Usage stats (Telegram)*\n\n"
        f"Messages: `{u['messages']}`\n"
        f"Input tokens: `{u['input']:,}`\n"
        f"Output tokens: `{u['output']:,}`",
        parse_mode="Markdown",
    )


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if cc.CLAUDE_MODE == "api":
        await update.message.reply_text(f"API mode uses model `{cc.CLAUDE_API_MODEL}`.", parse_mode="Markdown")
        return
    cur = _model(update.effective_user.id)
    btns = [
        [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:model:{k}")]
        for k, lbl in CLI_MODELS.items()
    ]
    await update.message.reply_text(
        f"Current model: *{cur}*\n\nSelect a model:",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_permissions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if cc.CLAUDE_MODE == "api":
        await update.message.reply_text("Permissions only apply in CLI mode.")
        return
    cur = _perm(update.effective_user.id) or "default"
    btns = [
        [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:perm:{k}")]
        for k, lbl in PERMISSION_MODES.items()
    ]
    await update.message.reply_text(
        f"Current permission mode: *{cur}*",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_verbose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = ctx.args
    if args and args[0] in ("0", "1", "2"):
        _user_verbose[uid] = int(args[0])
        await update.message.reply_text(f"Verbosity set to {args[0]}.")
        return
    cur = _verbose(uid)
    btns = [
        [InlineKeyboardButton(f"{lbl}{' ✓' if lvl==cur else ''}", callback_data=f"tg:verbose:{lvl}")]
        for lvl, lbl in VERBOSE_LEVELS.items()
    ]
    await update.message.reply_text(
        f"Current verbosity: *{cur}*",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


# ─── Callback buttons ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not _allowed(uid):
        return

    data = q.data  # e.g. "tg:model:sonnet"
    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    _, kind, value = parts

    # Handle cancel button (value is task_id)
    if kind == "cancel":
        task_id = int(value)
        task = _task_registry.get(task_id)
        if task and task.uid == uid:
            task.cancel()
            await q.edit_message_text("⏹ Cancelling…", reply_markup=None)
        return

    # Handle cancel all
    if kind == "cancelall":
        count = _task_registry.cancel_all_user(uid)
        await q.edit_message_text(f"⏹ Cancelling {count} task(s)…", reply_markup=None)
        return

    # Handle session management callbacks
    if kind == "sess":
        # value format: "action:param" e.g. "sw:2", "del:1", "new:_", "delmenu:_"
        sess_parts = value.split(":", 1)
        action = sess_parts[0]
        param = sess_parts[1] if len(sess_parts) > 1 else ""

        if action == "sw":
            idx = int(param)
            s = cc._session_mgr.switch_to(PLATFORM, str(uid), idx)
            if s:
                await q.edit_message_text(f"✅ Switched to `{s.name}`", parse_mode="Markdown")
            else:
                await q.edit_message_text("Session not found.")
        elif action == "new":
            name = f"session-{int(time.time()) % 10000}"
            s = cc._session_mgr.create(PLATFORM, str(uid), name)
            await q.edit_message_text(
                f"✅ Created and switched to `{s.name}`\n\nTip: use `/new <name>` to pick a name.",
                parse_mode="Markdown",
            )
        elif action == "delmenu":
            sessions = cc._session_mgr.get_all(PLATFORM, str(uid))
            active_idx = cc._session_mgr.get_active_index(PLATFORM, str(uid))
            btns = []
            for i, s in enumerate(sessions):
                if s.is_busy:
                    continue
                label = f"🗑 {s.name}" + (" (active)" if i == active_idx else "")
                btns.append([InlineKeyboardButton(label, callback_data=f"tg:sess:del:{i}")])
            btns.append([InlineKeyboardButton("↩️ Cancel", callback_data="tg:sess:back:_")])
            await q.edit_message_text(
                "Which session to delete?",
                reply_markup=InlineKeyboardMarkup(btns),
            )
        elif action == "del":
            idx = int(param)
            sessions = cc._session_mgr.get_all(PLATFORM, str(uid))
            name = sessions[idx].name if idx < len(sessions) else "?"
            if cc._session_mgr.delete(PLATFORM, str(uid), idx):
                await q.edit_message_text(f"🗑 Deleted session `{name}`", parse_mode="Markdown")
            else:
                await q.edit_message_text("Cannot delete (busy or not found).")
        elif action == "back":
            await q.edit_message_text("Cancelled.")
        return

    # Handle folder browser callbacks
    if kind in ("br", "bf", "bv", "ba"):
        await _handle_browse_callback(q, uid)
        return

    # Handle pagination
    if kind == "pg":
        # value = "rid:page"
        pg_parts = value.split(":")
        if len(pg_parts) < 2:
            return
        rid, page_num = pg_parts[0], int(pg_parts[1])
        resp = _response_store.get(rid)
        if not resp or resp["uid"] != uid:
            await q.edit_message_text("Response expired.", reply_markup=None)
            return

        text = resp["text"]
        total_pages = (len(text) + RESPONSE_PAGE_SIZE - 1) // RESPONSE_PAGE_SIZE
        page_num = min(page_num, total_pages - 1)
        start = page_num * RESPONSE_PAGE_SIZE
        page_text = text[start:start + RESPONSE_PAGE_SIZE]

        footer = f"\n\n📄 _Page {page_num + 1}/{total_pages}_"

        nav = []
        if page_num > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"tg:pg:{rid}:{page_num - 1}"))
        if page_num < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"tg:pg:{rid}:{page_num + 1}"))
        markup = InlineKeyboardMarkup([nav]) if nav else None

        try:
            await q.edit_message_text(page_text + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except Exception:
            await q.edit_message_text(page_text + footer, reply_markup=markup)
        return

    # Handle retry
    if kind == "retry":
        retry_key = f"retry_{value}"
        resp = _response_store.get(retry_key)
        if not resp or resp["uid"] != uid:
            await q.edit_message_text("Retry expired.", reply_markup=None)
            return

        prompt = resp["prompt"]
        del _response_store[retry_key]
        await q.edit_message_text("🔄 Retrying…", reply_markup=None)

        # Re-run as a task
        placeholder = q.message
        stop_evt = asyncio.Event()
        task = TaskSession(placeholder, uid, prompt)
        task.start_heartbeat()
        _task_registry.register(task)
        try:
            reply, stats = await _ask(uid, prompt, tracker=task)
            if task.cancelled:
                await placeholder.edit_text("⏹ Retry cancelled.", reply_markup=None)
                return
            retry_sess = _active_session(uid)
            cc.save_turn(PLATFORM, str(uid), prompt, reply, session_name=retry_sess.name)
            cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
            summary = task.finish_summary()
            reply = f"{summary}\n\n{reply}"
            md_text = _protect_urls_for_markdown(reply)
            if len(md_text) <= 4096:
                try:
                    await placeholder.edit_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
                except Exception:
                    await placeholder.edit_text(reply[:4096], reply_markup=None)
            else:
                rid = _store_response(uid, prompt, md_text)
                page_text = md_text[:RESPONSE_PAGE_SIZE]
                total_pages = (len(md_text) + RESPONSE_PAGE_SIZE - 1) // RESPONSE_PAGE_SIZE
                btns = [[InlineKeyboardButton("▶️ Next page", callback_data=f"tg:pg:{rid}:1")]]
                footer = f"\n\n📄 _Page 1/{total_pages}_"
                try:
                    await placeholder.edit_text(page_text + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(btns))
                except Exception:
                    await placeholder.edit_text(reply[:RESPONSE_PAGE_SIZE] + footer, reply_markup=InlineKeyboardMarkup(btns))
        except Exception as exc:
            retry_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry", callback_data=f"tg:retry:{task.task_id}")]])
            _response_store[f"retry_{task.task_id}"] = {"prompt": prompt, "uid": uid}
            await placeholder.edit_text(f"❌ Retry failed: {str(exc)[:150]}", reply_markup=retry_btn)
        finally:
            await task.stop()
            _task_registry.unregister(task.task_id)
        return

    if kind == "model":
        _user_model[uid] = value
        cur = _model(uid)
        btns = [
            [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:model:{k}")]
            for k, lbl in CLI_MODELS.items()
        ]
        await q.edit_message_text(
            f"Model set to: *{CLI_MODELS.get(value, value)}*",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown",
        )

    elif kind == "perm":
        _user_perm[uid] = "" if value == "default" else value
        cur = _perm(uid) or "default"
        btns = [
            [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:perm:{k}")]
            for k, lbl in PERMISSION_MODES.items()
        ]
        await q.edit_message_text(
            f"Permission mode set to: *{PERMISSION_MODES.get(value, value)}*",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown",
        )

    elif kind == "verbose":
        level = int(value)
        _user_verbose[uid] = level
        btns = [
            [InlineKeyboardButton(f"{lbl}{' ✓' if lvl==level else ''}", callback_data=f"tg:verbose:{lvl}")]
            for lvl, lbl in VERBOSE_LEVELS.items()
        ]
        await q.edit_message_text(
            f"Verbosity set to: *{level}* ({VERBOSE_LEVELS[level]})",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown",
        )

    elif kind == "engine":
        _user_engine[uid] = value
        cur = _engine(uid)
        btns = [
            [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:engine:{k}")]
            for k, lbl in ENGINE_MODES.items()
        ]
        await q.edit_message_text(
            f"Engine set to: *{ENGINE_MODES.get(value, value)}*",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown",
        )


# ─── Message handler ─────────────────────────────────────────────────────────────

async def _send_paginated(update: Update, uid: int, prompt: str, text: str, placeholder=None):
    """Send a response with pagination buttons if it's long."""
    chunk = 4096
    md_text = _protect_urls_for_markdown(text)

    # Short responses: send directly
    if len(md_text) <= chunk:
        if placeholder:
            try:
                await placeholder.edit_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
                return
            except Exception:
                try:
                    await placeholder.edit_text(text[:chunk], reply_markup=None)
                    return
                except Exception:
                    pass

        # Fallback: always try a new message
        try:
            await update.effective_message.reply_text(md_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.effective_message.reply_text(text[:chunk])
        return

    # Long responses: paginate
    rid = _store_response(uid, prompt, md_text)
    page_text = md_text[:RESPONSE_PAGE_SIZE]
    total_pages = (len(md_text) + RESPONSE_PAGE_SIZE - 1) // RESPONSE_PAGE_SIZE

    footer = f"\n\n📄 _Page 1/{total_pages}_"
    btns = [[InlineKeyboardButton("▶️ Next page", callback_data=f"tg:pg:{rid}:1")]]
    markup = InlineKeyboardMarkup(btns)

    if placeholder:
        try:
            await placeholder.edit_text(
                page_text + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
            return
        except Exception:
            try:
                await placeholder.edit_text(
                    text[:RESPONSE_PAGE_SIZE] + footer, reply_markup=markup
                )
                return
            except Exception:
                pass

    # Fallback: always send as new message (never silently lose a response)
    try:
        await update.effective_message.reply_text(
            page_text + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
        )
    except Exception:
        try:
            await update.effective_message.reply_text(text[:RESPONSE_PAGE_SIZE], reply_markup=markup)
        except Exception:
            log.error("Failed to send response for uid=%s, text len=%d", uid, len(text))


async def _run_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, user_text: str):
    """Execute a single Claude task. Can run concurrently with other tasks."""
    placeholder = await update.message.reply_text("🧠 Thinking…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))

    # Create task session with progress tracking
    task = TaskSession(placeholder, uid, user_text)
    task.chat_id = update.effective_chat.id
    task.bot = ctx.bot
    task.start_heartbeat()
    _task_registry.register(task)

    sess = _active_session(uid)
    sess.is_busy = True
    try:
        reply, stats = await _ask(uid, user_text, tracker=task, session=sess)

        if task.cancelled:
            await placeholder.edit_text(
                f"⏹ Task cancelled after {task._elapsed()}.", reply_markup=None,
            )
            return

        # Stop heartbeat before sending final response to avoid edit race
        await task.stop()

        is_timeout = reply.startswith("[Timeout]")
        is_error = reply.startswith("[Claude error]")

        if not reply or not reply.strip():
            reply = "(No response from Claude — the task may have completed without output.)"
            log.warning("Empty reply for uid=%s, task #%d", uid, task.task_id)

        # Don't save timeouts/errors to conversation history — they pollute context
        if not is_timeout and not is_error:
            cc.save_turn(PLATFORM, str(uid), user_text, reply, session_name=sess.name)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        cc.track_tool_usage(PLATFORM, str(uid), stats.get("tools_used", []))
        cc.track_cost(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0), stats.get("cost_usd", 0))

        # Build header with completion stats
        v = _verbose(uid)
        if is_timeout:
            # Show timeout with retry button instead of success summary
            retry_btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=f"tg:retry:{task.task_id}")],
            ])
            _response_store[f"retry_{task.task_id}"] = {"prompt": user_text, "uid": uid}
            # Include any partial results from streaming
            partial = task._partial_text.strip() if task._partial_text else ""
            timeout_msg = f"⏱ *Timed out* after {task._elapsed()}"
            if task.tool_count:
                timeout_msg += f" ({task.tool_count} tools used)"
            if partial:
                preview = partial[:1500]
                if len(partial) > 1500:
                    preview += "\n\n⏳ _(partial result — task timed out before finishing)_"
                timeout_msg += f"\n\n{preview}"
            try:
                await placeholder.edit_text(
                    timeout_msg, reply_markup=retry_btn, parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                try:
                    await placeholder.edit_text(
                        f"⏱ Timed out after {task._elapsed()}\n\n{partial[:1500] if partial else reply}",
                        reply_markup=retry_btn,
                    )
                except Exception:
                    await update.effective_message.reply_text(
                        f"⏱ Timed out after {task._elapsed()}", reply_markup=retry_btn,
                    )
            return

        summary = task.finish_summary()
        if v >= 1 and stats.get("tools_used"):
            tools_str = ', '.join(stats['tools_used'][:5])
            if len(stats['tools_used']) > 5:
                tools_str += f" +{len(stats['tools_used']) - 5} more"
            reply = f"{summary}\n🔧 _{tools_str}_\n\n{reply}"
        elif v >= 1:
            reply = f"{summary}\n\n{reply}"
        if v >= 2 and stats.get("input_tokens"):
            cost = stats.get("cost_usd", 0)
            cost_str = f" · ${cost:.4f}" if cost else ""
            reply += f"\n\n_({stats['input_tokens']}→{stats['output_tokens']} tokens{cost_str})_"

        await _send_paginated(update, uid, user_text, reply, placeholder=placeholder)

        # Notification ping for long tasks (>30s) — vibrates the phone
        elapsed_secs = time.time() - task.start_time
        if elapsed_secs > 30:
            try:
                # Include a short preview so the user sees what happened
                raw_reply = reply
                # Strip the prepended summary/tools header to get actual content
                if "\n\n" in raw_reply:
                    raw_reply = raw_reply.split("\n\n", 1)[1]
                preview = raw_reply[:200].strip()
                if len(raw_reply) > 200:
                    preview += "…"
                await update.effective_message.reply_text(
                    f"🔔 Done ({task._elapsed()})\n\n{preview}",
                    disable_notification=False,
                )
            except Exception:
                pass

    except asyncio.CancelledError:
        await placeholder.edit_text(
            f"⏹ Task cancelled after {task._elapsed()}.", reply_markup=None,
        )
    except Exception as exc:
        log.exception("handle_message error (task #%d)", task.task_id)
        error_msg = str(exc)[:200]
        # Show error with retry button
        retry_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry", callback_data=f"tg:retry:{task.task_id}")],
        ])
        # Store the prompt for retry
        _response_store[f"retry_{task.task_id}"] = {"prompt": user_text, "uid": uid}
        try:
            await placeholder.edit_text(
                f"❌ Error after {task._elapsed()}: {error_msg}",
                reply_markup=retry_btn,
            )
        except Exception:
            await update.message.reply_text(f"❌ Error: {error_msg}", reply_markup=retry_btn)
    finally:
        sess.is_busy = False
        await task.stop()
        _task_registry.unregister(task.task_id)
        stop.set()
        await typing_task


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        await update.message.reply_text("You're not authorized.")
        return

    # Deduplicate: Telegram can send the same message multiple times on retries
    if _is_duplicate(update.message.message_id):
        return

    if not cc.check_rate_limit(f"tg:{uid}"):
        await update.message.reply_text(
            f"Rate limit: max {cc.RATE_LIMIT_REQUESTS} messages per {cc.RATE_LIMIT_WINDOW}s."
        )
        return

    user_text = update.message.text or ""
    if not user_text.strip():
        return

    # Check concurrent task limit
    if _task_registry.user_task_count(uid) >= MAX_CONCURRENT_TASKS:
        active = _task_registry.get_user_tasks(uid)
        task_list = "\n".join(
            f"  `#{t.task_id}` — {t.prompt_preview}… ({t._elapsed()})"
            for t in active
        )
        await update.message.reply_text(
            f"⚠️ You have {len(active)} tasks running (max {MAX_CONCURRENT_TASKS}).\n\n"
            f"{task_list}\n\n"
            f"Use /tasks to manage or /cancel to stop one.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Fire and forget — task runs concurrently
    asyncio.create_task(_run_task(update, ctx, uid, user_text))


# ─── Upload directory for persistent file storage ─────────────────────────────

UPLOAD_DIR = Path(cc.CLAUDE_WORK_DIR) / "telegram_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


def _upload_path(uid: int, suffix: str, prefix: str = "") -> Path:
    """Return a persistent path for an uploaded file."""
    ts = int(time.time() * 1000)
    name = f"{prefix}{uid}_{ts}{suffix}"
    return UPLOAD_DIR / name


# ─── Photo handler ───────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not cc.check_rate_limit(f"tg:{uid}"):
        await update.message.reply_text("Rate limit exceeded.")
        return

    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    raw = bytes(await file.download_as_bytearray())
    caption = update.message.caption or "Analyze this image."

    save_path = _upload_path(uid, ".jpg", "img_")
    save_path.write_bytes(raw)
    prompt = f"{caption}\n\n[Image saved at: {save_path}]"

    placeholder = await update.message.reply_text("🔍 Analyzing image…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))
    task = TaskSession(placeholder, uid, f"[Image] {caption}")
    task.start_heartbeat()
    _task_registry.register(task)

    try:
        reply, stats = await _ask(uid, prompt, tracker=task)

        if task.cancelled:
            await placeholder.edit_text(
                f"⏹ Task cancelled. `#{task.task_id}`", reply_markup=None, parse_mode=ParseMode.MARKDOWN
            )
            return

        img_sess = _active_session(uid)
        cc.save_turn(PLATFORM, str(uid), f"[Image at {save_path}] {caption}", reply, session_name=img_sess.name)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_photo error")
        await placeholder.edit_text(f"Error: {exc}", reply_markup=None)
    finally:
        await task.stop()
        _task_registry.unregister(task.task_id)
        stop.set()
        await typing_task


# ─── Document handler ────────────────────────────────────────────────────────────

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not cc.check_rate_limit(f"tg:{uid}"):
        await update.message.reply_text("Rate limit exceeded.")
        return

    doc = update.message.document
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("File too large (max 10 MB).")
        return

    file = await ctx.bot.get_file(doc.file_id)
    raw = bytes(await file.download_as_bytearray())
    filename = doc.file_name or "file"
    caption = update.message.caption or f"Analyze this file: {filename}"

    suffix = Path(filename).suffix or ".txt"
    save_path = _upload_path(uid, suffix, "doc_")
    save_path.write_bytes(raw)
    prompt = f"{caption}\n\n[File '{filename}' saved at: {save_path}]"

    placeholder = await update.message.reply_text(f"📄 Processing {filename}…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))
    task = TaskSession(placeholder, uid, f"[File] {filename}")
    task.start_heartbeat()
    _task_registry.register(task)

    try:
        reply, stats = await _ask(uid, prompt, tracker=task)

        if task.cancelled:
            await placeholder.edit_text(
                f"⏹ Task cancelled. `#{task.task_id}`", reply_markup=None, parse_mode=ParseMode.MARKDOWN
            )
            return

        doc_sess = _active_session(uid)
        cc.save_turn(PLATFORM, str(uid), f"[File '{filename}' at {save_path}] {caption}", reply, session_name=doc_sess.name)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_document error")
        await placeholder.edit_text(f"Error: {exc}", reply_markup=None)
    finally:
        await task.stop()
        _task_registry.unregister(task.task_id)
        stop.set()
        await typing_task


# ─── Entry point ─────────────────────────────────────────────────────────────────

def build_app() -> Application:
    cc.init_db()

    import ssl
    from telegram.request import HTTPXRequest
    req = HTTPXRequest(
        http_version="1.1",
        connection_pool_size=8,
        httpx_kwargs={"verify": False},   # needed on some corporate networks
    )
    app = Application.builder().token(TOKEN).request(req).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("tasks",       cmd_tasks))
    app.add_handler(CommandHandler("cancel",      cmd_cancel))
    app.add_handler(CommandHandler("sessions",    cmd_sessions))
    app.add_handler(CommandHandler("new",         cmd_new))
    app.add_handler(CommandHandler("switch",      cmd_switch))
    app.add_handler(CommandHandler("browse",      cmd_browse))
    app.add_handler(CommandHandler("reset",       cmd_reset))
    app.add_handler(CommandHandler("mode",        cmd_mode))
    app.add_handler(CommandHandler("model",       cmd_model))
    app.add_handler(CommandHandler("engine",      cmd_engine))
    app.add_handler(CommandHandler("verbose",     cmd_verbose))
    app.add_handler(CommandHandler("permissions", cmd_permissions))
    app.add_handler(CommandHandler("usage",       cmd_usage))
    app.add_handler(CommandHandler("id",          cmd_id))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^tg:"))
    app.add_handler(MessageHandler(filters.PHOTO,              handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL,       handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


async def run_telegram():
    log.info("Telegram bot starting…")
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log.info("Telegram bot is running.")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

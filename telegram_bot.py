"""
Telegram adapter — wraps claude_core for the Telegram Bot API.
Run via main.py with BOT_MODE=telegram or BOT_MODE=both.
"""

import asyncio
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


# ─── Progress tracker for long-running tasks ──────────────────────────────────

class ProgressTracker:
    """Manages live progress updates for a running Claude task."""

    CANCEL_CALLBACK = "tg:cancel"

    def __init__(self, placeholder, uid: int):
        self.placeholder = placeholder
        self.uid = uid
        self.start_time = time.time()
        self.tools: list[str] = []
        self.tool_count = 0
        self._last_update = 0.0
        self._cancelled = False
        self._heartbeat_task: asyncio.Task | None = None

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
        """Animated progress indicator based on elapsed time."""
        secs = int(time.time() - self.start_time)
        frames = ["◐", "◓", "◑", "◒"]
        return frames[secs % 4]

    def _build_status(self) -> str:
        elapsed = self._elapsed()
        spinner = self._progress_bar()
        lines = [f"{spinner} *Working…* `{elapsed}`"]

        if self.tools:
            # Show recent tool activity (last 5 unique tools)
            unique_recent = list(dict.fromkeys(self.tools[-8:]))[-5:]
            tool_lines = [_tool_label(t) for t in unique_recent]
            lines.append("")
            lines.extend(tool_lines)

        if self.tool_count > 5:
            lines.append(f"\n_({self.tool_count} tool calls total)_")

        return "\n".join(lines)

    async def on_tool(self, tool_name: str):
        """Called when Claude starts using a tool."""
        self.tools.append(tool_name)
        self.tool_count += 1
        await self._update()

    async def _update(self):
        """Update the placeholder message (rate-limited to avoid Telegram 429s)."""
        now = time.time()
        # Rate limit updates to once per 2 seconds
        if now - self._last_update < 2.0:
            return
        self._last_update = now

        status = self._build_status()
        cancel_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ Cancel", callback_data=f"{self.CANCEL_CALLBACK}:{self.uid}")]
        ])
        try:
            await self.placeholder.edit_text(
                status,
                reply_markup=cancel_btn,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    async def _heartbeat(self):
        """Periodic updates even without tool activity — keeps user informed."""
        await asyncio.sleep(5)  # First heartbeat after 5s
        while not self._cancelled:
            await self._update()
            # Increase interval as task runs longer
            elapsed = time.time() - self.start_time
            if elapsed < 30:
                interval = 5
            elif elapsed < 120:
                interval = 10
            else:
                interval = 15
            await asyncio.sleep(interval)

    def start_heartbeat(self):
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def stop(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

    def finish_summary(self) -> str:
        """Return a brief summary line showing work done."""
        elapsed = self._elapsed()
        if self.tool_count > 0:
            return f"⚡ _{self.tool_count} tools · {elapsed}_"
        return f"⚡ _{elapsed}_"


# Active progress trackers by user (for cancel support)
_active_tasks: dict[int, ProgressTracker] = {}


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
        # Wrap bare URL as clickable markdown link
        return f"[{url}]({url})"

    return _URL_RE.sub(_replace_bare_url, text)


async def _send(placeholder, update: Update, text: str):
    chunk = 4096
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
        # Fallback: plain text (Telegram auto-links bare URLs in plain text)
        if len(text) <= chunk:
            await placeholder.edit_text(text, reply_markup=None)
        else:
            await placeholder.edit_text(text[:chunk], reply_markup=None)
            for i in range(chunk, len(text), chunk):
                await update.effective_message.reply_text(text[i:i+chunk])


# ─── Tool name → friendly emoji/label ──────────────────────────────────────────

_TOOL_LABELS = {
    "Bash":      "🖥️ Running command…",
    "Read":      "📖 Reading file…",
    "Write":     "📝 Writing file…",
    "Edit":      "✏️ Editing file…",
    "Grep":      "🔍 Searching…",
    "WebSearch": "🌐 Searching the web…",
    "WebFetch":  "🌐 Fetching page…",
    "Agent":     "🤖 Delegating to agent…",
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


async def _ask(uid: int, text: str, tracker: ProgressTracker | None = None) -> tuple[str, dict]:
    history = cc.load_history(PLATFORM, str(uid))
    engine = _engine(uid)

    if engine == "api":
        return cc.ask_claude_api(text, history, system=cc.CLAUDE_SYSTEM)

    # Progress callback via tracker
    async def _on_progress(tool_name: str):
        if tracker:
            await tracker.on_tool(tool_name)

    # Cancellation check
    def _is_cancelled() -> bool:
        return tracker.cancelled if tracker else False

    if engine == "sdk":
        return await cc.ask_claude_sdk(
            text, history,
            model=_model(uid),
            system=cc.CLAUDE_SYSTEM,
            add_dirs=cc.CLAUDE_ADD_DIRS,
            timeout=cc.CLAUDE_TIMEOUT,
            on_progress=_on_progress,
            is_cancelled=_is_cancelled,
        )

    # Default: CLI mode
    return await cc.ask_claude_async(
        text, history,
        model=_model(uid),
        system=cc.CLAUDE_SYSTEM,
        add_dirs=cc.CLAUDE_ADD_DIRS,
        perm_mode=_perm(uid),
        timeout=cc.CLAUDE_TIMEOUT,
        on_progress=_on_progress,
        is_cancelled=_is_cancelled,
    )


# ─── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm Claude on Telegram.\n\n"
        "Commands:\n"
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
    cc.clear_history(PLATFORM, str(update.effective_user.id))
    await update.message.reply_text("Conversation history cleared.")


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    info = (
        f"Platform: `telegram`\n"
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

    # Handle cancel button
    if kind == "cancel":
        target_uid = int(value)
        if target_uid == uid and target_uid in _active_tasks:
            _active_tasks[target_uid].cancel()
            await q.edit_message_text("⏹ Cancelling…", reply_markup=None)
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

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        await update.message.reply_text("You're not authorized.")
        return
    if not cc.check_rate_limit(f"tg:{uid}"):
        await update.message.reply_text(
            f"Rate limit: max {cc.RATE_LIMIT_REQUESTS} messages per {cc.RATE_LIMIT_WINDOW}s."
        )
        return

    user_text = update.message.text or ""
    if not user_text.strip():
        return

    placeholder = await update.message.reply_text("⏳ Thinking…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))

    # Set up progress tracker with heartbeat and cancel support
    tracker = ProgressTracker(placeholder, uid)
    tracker.start_heartbeat()
    _active_tasks[uid] = tracker

    try:
        reply, stats = await _ask(uid, user_text, tracker=tracker)

        if tracker.cancelled:
            await placeholder.edit_text("⏹ Task cancelled.", reply_markup=None)
            return

        cc.save_turn(PLATFORM, str(uid), user_text, reply)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        cc.track_tool_usage(PLATFORM, str(uid), stats.get("tools_used", []))
        cc.track_cost(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0), stats.get("cost_usd", 0))

        v = _verbose(uid)
        if v >= 1 and stats.get("tools_used"):
            summary = tracker.finish_summary()
            reply = f"{summary}\n🔧 _{', '.join(stats['tools_used'][:5])}_\n\n{reply}"
        elif v >= 1 and tracker.tool_count == 0:
            summary = tracker.finish_summary()
            reply = f"{summary}\n\n{reply}"
        if v >= 2 and stats.get("input_tokens"):
            cost = stats.get("cost_usd", 0)
            cost_str = f" · ${cost:.4f}" if cost else ""
            reply += f"\n\n_({stats['input_tokens']}→{stats['output_tokens']} tokens{cost_str})_"

        await _send(placeholder, update, reply)
    except asyncio.CancelledError:
        await placeholder.edit_text("⏹ Task cancelled.", reply_markup=None)
    except Exception as exc:
        log.exception("handle_message error")
        await placeholder.edit_text(f"Error: {exc}", reply_markup=None)
    finally:
        await tracker.stop()
        _active_tasks.pop(uid, None)
        stop.set()
        await typing_task


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

    placeholder = await update.message.reply_text("🔍 Analyzing image…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))
    tracker = ProgressTracker(placeholder, uid)
    tracker.start_heartbeat()
    _active_tasks[uid] = tracker

    try:
        save_path = _upload_path(uid, ".jpg", "img_")
        save_path.write_bytes(raw)

        prompt = f"{caption}\n\n[Image saved at: {save_path}]"
        reply, stats = await _ask(uid, prompt, tracker=tracker)

        if tracker.cancelled:
            await placeholder.edit_text("⏹ Task cancelled.", reply_markup=None)
            return

        cc.save_turn(PLATFORM, str(uid), f"[Image at {save_path}] {caption}", reply)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_photo error")
        await placeholder.edit_text(f"Error: {exc}", reply_markup=None)
    finally:
        await tracker.stop()
        _active_tasks.pop(uid, None)
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

    placeholder = await update.message.reply_text(f"📄 Processing {filename}…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))
    tracker = ProgressTracker(placeholder, uid)
    tracker.start_heartbeat()
    _active_tasks[uid] = tracker

    try:
        suffix = Path(filename).suffix or ".txt"
        save_path = _upload_path(uid, suffix, "doc_")
        save_path.write_bytes(raw)

        prompt = f"{caption}\n\n[File '{filename}' saved at: {save_path}]"
        reply, stats = await _ask(uid, prompt, tracker=tracker)

        if tracker.cancelled:
            await placeholder.edit_text("⏹ Task cancelled.", reply_markup=None)
            return

        cc.save_turn(PLATFORM, str(uid), f"[File '{filename}' at {save_path}] {caption}", reply)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_document error")
        await placeholder.edit_text(f"Error: {exc}", reply_markup=None)
    finally:
        await tracker.stop()
        _active_tasks.pop(uid, None)
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

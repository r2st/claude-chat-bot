"""
Telegram adapter — wraps claude_core for the Telegram Bot API.
Run via main.py with BOT_MODE=telegram or BOT_MODE=both.
"""

import asyncio
import logging
import os
import tempfile
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


# ─── Reply helper ───────────────────────────────────────────────────────────────

async def _send(placeholder, update: Update, text: str):
    chunk = 4096
    try:
        if len(text) <= chunk:
            await placeholder.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await placeholder.edit_text(text[:chunk], parse_mode=ParseMode.MARKDOWN)
            for i in range(chunk, len(text), chunk):
                await update.effective_message.reply_text(text[i:i+chunk], parse_mode=ParseMode.MARKDOWN)
    except Exception:
        if len(text) <= chunk:
            await placeholder.edit_text(text)
        else:
            await placeholder.edit_text(text[:chunk])
            for i in range(chunk, len(text), chunk):
                await update.effective_message.reply_text(text[i:i+chunk])


# ─── Core ask ───────────────────────────────────────────────────────────────────

async def _ask(uid: int, text: str) -> tuple[str, dict]:
    history = cc.load_history(PLATFORM, str(uid))
    if cc.CLAUDE_MODE == "api":
        return cc.ask_claude_api(text, history, system=cc.CLAUDE_SYSTEM)
    return await cc.ask_claude_async(
        text, history,
        model=_model(uid),
        system=cc.CLAUDE_SYSTEM,
        add_dirs=cc.CLAUDE_ADD_DIRS,
        perm_mode=_perm(uid),
        timeout=cc.CLAUDE_TIMEOUT,
    )


# ─── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm Claude on Telegram.\n\n"
        "Commands:\n"
        "/reset — clear conversation history\n"
        "/model — switch Claude model\n"
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
        f"Claude mode: `{cc.CLAUDE_MODE}`\n"
        f"Model: `{_model(uid)}`\n"
        f"Permission mode: `{_perm(uid) or 'default'}`\n"
        f"Verbose: `{_verbose(uid)}`\n"
        f"Timeout: `{cc.CLAUDE_TIMEOUT}s`"
    )
    await update.message.reply_text(info, parse_mode="Markdown")


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
    _, kind, value = data.split(":", 2)

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

    try:
        reply, stats = await _ask(uid, user_text)
        cc.save_turn(PLATFORM, str(uid), user_text, reply)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))

        v = _verbose(uid)
        if v >= 1 and stats.get("tools_used"):
            reply = f"🔧 _{', '.join(stats['tools_used'][:5])}_\n\n{reply}"
        if v >= 2 and stats.get("input_tokens"):
            reply += f"\n\n_({stats['input_tokens']}→{stats['output_tokens']} tokens)_"

        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_message error")
        await placeholder.edit_text(f"Error: {exc}")
    finally:
        stop.set()
        await typing_task


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

    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, dir=cc.CLAUDE_WORK_DIR) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name

        prompt = f"{caption}\n\n[Image saved at: {tmp_path}]"
        reply, stats = await _ask(uid, prompt)
        import os; os.unlink(tmp_path)

        cc.save_turn(PLATFORM, str(uid), f"[Image] {caption}", reply)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_photo error")
        await placeholder.edit_text(f"Error: {exc}")
    finally:
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

    try:
        suffix = Path(filename).suffix or ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=cc.CLAUDE_WORK_DIR, prefix="tg_") as tmp:
            tmp.write(raw)
            tmp_path = tmp.name

        prompt = f"{caption}\n\n[File '{filename}' saved at: {tmp_path}]"
        reply, stats = await _ask(uid, prompt)
        import os; os.unlink(tmp_path)

        cc.save_turn(PLATFORM, str(uid), f"[File: {filename}] {caption}", reply)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_document error")
        await placeholder.edit_text(f"Error: {exc}")
    finally:
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
    await app.run_polling()

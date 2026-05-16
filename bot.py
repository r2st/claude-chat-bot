import os
import asyncio
import json
import logging
import tempfile
import time
import sqlite3
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode, ChatAction

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

CLAUDE_MODE = os.environ.get("CLAUDE_MODE", "cli")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful assistant accessible via Telegram.")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "120"))

CLAUDE_CLI_WORK_DIR = os.environ.get("CLAUDE_CLI_WORK_DIR", os.path.expanduser("~"))
CLAUDE_CLI_ADD_DIRS = os.environ.get("CLAUDE_CLI_ADD_DIRS", "")

ALLOWED_USER_IDS = set()
raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
if raw_ids:
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip()}

RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "10"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "bot.db"))

PERMISSION_MODES = {
    "default": "Default (prompts for everything)",
    "acceptEdits": "Accept Edits (auto-approve file read/write)",
    "auto": "Auto (auto-approve most actions)",
    "bypassPermissions": "Bypass All (no restrictions)",
}

CLI_MODELS = {
    "haiku": "Haiku (fastest)",
    "sonnet": "Sonnet (balanced)",
    "opus": "Opus (most capable)",
}

VERBOSE_LEVELS = {0: "Quiet", 1: "Normal", 2: "Detailed"}

user_permission_mode: dict[int, str] = {}
user_cli_model: dict[int, str] = {}
user_verbose: dict[int, int] = {}
user_rate_limits: dict[int, list[float]] = {}

_default_perm = os.environ.get("CLAUDE_CLI_PERMISSION_MODE", "")
_default_cli_model = os.environ.get("CLAUDE_CLI_MODEL", "sonnet")

_api_client = None


# --- Database ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp REAL,
            PRIMARY KEY (user_id, timestamp)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER PRIMARY KEY,
            message_count INTEGER DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def load_conversation(user_id: int) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, content FROM conversations WHERE user_id = ? ORDER BY timestamp",
        (user_id,)
    ).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def save_message(user_id: int, role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO conversations (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, role, content, time.time())
    )
    rows = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    if rows > 20:
        conn.execute("""
            DELETE FROM conversations WHERE user_id = ? AND timestamp IN (
                SELECT timestamp FROM conversations WHERE user_id = ? ORDER BY timestamp LIMIT ?
            )
        """, (user_id, user_id, rows - 20))
    conn.commit()
    conn.close()


def clear_conversation(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def track_usage(user_id: int, input_tokens: int = 0, output_tokens: int = 0):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO usage (user_id, message_count, total_input_tokens, total_output_tokens)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            message_count = message_count + 1,
            total_input_tokens = total_input_tokens + excluded.total_input_tokens,
            total_output_tokens = total_output_tokens + excluded.total_output_tokens
    """, (user_id, input_tokens, output_tokens))
    conn.commit()
    conn.close()


def get_usage(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT message_count, total_input_tokens, total_output_tokens FROM usage WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    if row:
        return {"messages": row[0], "input_tokens": row[1], "output_tokens": row[2]}
    return {"messages": 0, "input_tokens": 0, "output_tokens": 0}


# --- Helpers ---

def get_api_client():
    global _api_client
    if _api_client is None:
        try:
            import anthropic
            _api_client = anthropic.Anthropic()
        except ImportError:
            raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    return _api_client


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    if user_id not in user_rate_limits:
        user_rate_limits[user_id] = []
    timestamps = user_rate_limits[user_id]
    user_rate_limits[user_id] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(user_rate_limits[user_id]) >= RATE_LIMIT_REQUESTS:
        return False
    user_rate_limits[user_id].append(now)
    return True


def get_permission_mode(user_id: int) -> str:
    return user_permission_mode.get(user_id, _default_perm)


def get_cli_model(user_id: int) -> str:
    return user_cli_model.get(user_id, _default_cli_model)


def get_verbose(user_id: int) -> int:
    return user_verbose.get(user_id, 1)


async def send_typing(chat_id: int, context: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.5)
            break
        except asyncio.TimeoutError:
            continue


async def send_response(placeholder, update, text: str):
    try:
        if len(text) <= 4096:
            await placeholder.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await placeholder.edit_text(text[:4096], parse_mode=ParseMode.MARKDOWN)
            for i in range(4096, len(text), 4096):
                await update.effective_message.reply_text(text[i:i + 4096], parse_mode=ParseMode.MARKDOWN)
    except Exception:
        if len(text) <= 4096:
            await placeholder.edit_text(text)
        else:
            await placeholder.edit_text(text[:4096])
            for i in range(4096, len(text), 4096):
                await update.effective_message.reply_text(text[i:i + 4096])


# --- Claude backends ---

async def call_claude_cli(prompt: str, history: list[dict], user_id: int, files: list[str] = None) -> tuple[str, dict]:
    full_prompt = ""
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        full_prompt += f"{role}: {msg['content']}\n\n"
    full_prompt += f"User: {prompt}"

    if files:
        full_prompt += "\n\n[Attached files: " + ", ".join(files) + "]"

    model = get_cli_model(user_id)
    cmd = ["claude", "-p", full_prompt, "--model", model, "--verbose", "--output-format", "stream-json"]
    perm = get_permission_mode(user_id)
    if perm:
        cmd.extend(["--permission-mode", perm])
    for d in CLAUDE_CLI_ADD_DIRS.split(","):
        d = d.strip()
        if d:
            cmd.extend(["--add-dir", d])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=CLAUDE_CLI_WORK_DIR,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Claude timed out after {CLAUDE_TIMEOUT}s")

    output = stdout.decode().strip()
    if proc.returncode != 0:
        error = stderr.decode().strip() or output
        logger.error(f"Claude CLI error (exit {proc.returncode}): {error}")
        raise RuntimeError(f"CLI error: {error}")
    result_text = ""
    tools_used = []
    stats = {}

    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            result_text += line
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
        elif etype == "assistant" and "message" in event:
            msg = event["message"]
            if isinstance(msg, dict):
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        result_text = block.get("text", "")
                    elif block.get("type") == "tool_use":
                        tools_used.append(block.get("name", "tool"))
        elif etype == "content_block_start":
            cb = event.get("content_block", {})
            if cb.get("type") == "tool_use":
                tools_used.append(cb.get("name", "tool"))

    if not result_text:
        result_text = output

    stats["tools_used"] = tools_used
    return result_text, stats


def call_claude_api(messages: list[dict], files: list[dict] = None) -> tuple[str, dict]:
    client = get_api_client()

    api_messages = []
    for msg in messages:
        if isinstance(msg.get("content"), list):
            api_messages.append(msg)
        else:
            api_messages.append({"role": msg["role"], "content": msg["content"]})

    if files:
        last_msg = api_messages[-1]
        content_blocks = []
        if isinstance(last_msg["content"], str):
            content_blocks.append({"type": "text", "text": last_msg["content"]})
        else:
            content_blocks = list(last_msg["content"])
        for f in files:
            if f["type"] == "image":
                content_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": f["media_type"], "data": f["data"]}
                })
            else:
                content_blocks.append({"type": "text", "text": f"[File: {f['name']}]\n{f['data']}"})
        api_messages[-1] = {"role": last_msg["role"], "content": content_blocks}

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=api_messages,
    )
    text = response.content[0].text
    stats = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "tools_used": [],
    }
    return text, stats


async def call_claude(prompt: str, history: list[dict], user_id: int, files=None) -> tuple[str, dict]:
    if CLAUDE_MODE == "api":
        messages = history + [{"role": "user", "content": prompt}]
        return call_claude_api(messages, files=files)
    file_paths = [f["path"] for f in (files or []) if "path" in f]
    return await call_claude_cli(prompt, history, user_id, files=file_paths)


# --- Commands ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode_label = "Claude CLI" if CLAUDE_MODE == "cli" else f"Claude API ({CLAUDE_MODEL})"
    await update.message.reply_text(
        f"Hi! I'm a Claude bot powered by {mode_label}.\n\n"
        "Send me any message and I'll respond.\n"
        "You can also send photos and files for analysis.\n\n"
        "Commands:\n"
        "/reset - Clear conversation history\n"
        "/mode - Show current mode and model\n"
        "/model - Switch Claude model\n"
        "/verbose - Set output verbosity (0/1/2)\n"
        "/permissions - Change CLI permission mode\n"
        "/usage - Show usage statistics\n"
        "/id - Show your Telegram user ID"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your user ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    perm = get_permission_mode(user_id) or "default"
    verbose = get_verbose(user_id)
    if CLAUDE_MODE == "cli":
        model = get_cli_model(user_id)
        info = (f"Mode: `cli`\nModel: `{model}`\nPermissions: `{perm}`\n"
                f"Verbose: `{verbose}`\nTimeout: `{CLAUDE_TIMEOUT}s`")
    else:
        info = f"Mode: `api`\nModel: `{CLAUDE_MODEL}`\nMax tokens: `{MAX_TOKENS}`\nVerbose: `{verbose}`"
    await update.message.reply_text(info, parse_mode="Markdown")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_conversation(update.effective_user.id)
    await update.message.reply_text("Conversation cleared.")


async def cmd_verbose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] in ("0", "1", "2"):
        level = int(args[0])
        user_verbose[user_id] = level
        await update.message.reply_text(f"Verbosity set to {level} ({VERBOSE_LEVELS[level]})")
    else:
        current = get_verbose(user_id)
        buttons = []
        for lvl, label in VERBOSE_LEVELS.items():
            marker = " ✓" if lvl == current else ""
            buttons.append([InlineKeyboardButton(f"{label}{marker}", callback_data=f"verbose:{lvl}")])
        await update.message.reply_text(
            f"Current verbosity: *{current}* ({VERBOSE_LEVELS[current]})\n\n"
            "0 = Final response only\n1 = Tool names shown\n2 = Detailed tool info",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usage = get_usage(user_id)
    text = (
        f"📊 *Usage Statistics*\n\n"
        f"Messages: `{usage['messages']}`\n"
        f"Input tokens: `{usage['input_tokens']:,}`\n"
        f"Output tokens: `{usage['output_tokens']:,}`\n"
        f"Total tokens: `{usage['input_tokens'] + usage['output_tokens']:,}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if CLAUDE_MODE != "cli":
        await update.message.reply_text(
            f"API mode uses model `{CLAUDE_MODEL}` (set via CLAUDE\\_MODEL env var).",
            parse_mode="Markdown",
        )
        return

    current = get_cli_model(update.effective_user.id)
    buttons = []
    for key, label in CLI_MODELS.items():
        marker = " ✓" if key == current else ""
        buttons.append([InlineKeyboardButton(f"{label}{marker}", callback_data=f"model:{key}")])

    await update.message.reply_text(
        f"Current model: *{current}*\n\nSelect a model:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cmd_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if CLAUDE_MODE != "cli":
        await update.message.reply_text("Permission modes only apply to CLI mode.")
        return

    current = get_permission_mode(update.effective_user.id) or "default"
    buttons = []
    for mode_key, mode_label in PERMISSION_MODES.items():
        marker = " ✓" if mode_key == current else ""
        buttons.append([InlineKeyboardButton(f"{mode_label}{marker}", callback_data=f"perm:{mode_key}")])

    await update.message.reply_text(
        f"Current permission mode: *{current}*\n\nSelect a mode:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# --- Callbacks ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_allowed(user_id):
        return

    data = query.data

    if data.startswith("perm:"):
        mode = data.split(":", 1)[1]
        user_permission_mode[user_id] = mode if mode != "default" else ""
        label = PERMISSION_MODES.get(mode, mode)
        current = get_permission_mode(user_id) or "default"
        buttons = []
        for mode_key, mode_label in PERMISSION_MODES.items():
            marker = " ✓" if mode_key == current else ""
            buttons.append([InlineKeyboardButton(f"{mode_label}{marker}", callback_data=f"perm:{mode_key}")])
        await query.edit_message_text(
            f"Permission mode set to: *{label}*\n\nSelect a mode:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )

    elif data.startswith("model:"):
        model = data.split(":", 1)[1]
        user_cli_model[user_id] = model
        label = CLI_MODELS.get(model, model)
        current = get_cli_model(user_id)
        buttons = []
        for key, mlabel in CLI_MODELS.items():
            marker = " ✓" if key == current else ""
            buttons.append([InlineKeyboardButton(f"{mlabel}{marker}", callback_data=f"model:{key}")])
        await query.edit_message_text(
            f"Model set to: *{label}*\n\nSelect a model:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )

    elif data.startswith("verbose:"):
        level = int(data.split(":", 1)[1])
        user_verbose[user_id] = level
        buttons = []
        for lvl, label in VERBOSE_LEVELS.items():
            marker = " ✓" if lvl == level else ""
            buttons.append([InlineKeyboardButton(f"{label}{marker}", callback_data=f"verbose:{lvl}")])
        await query.edit_message_text(
            f"Verbosity set to: *{level}* ({VERBOSE_LEVELS[level]})\n\n"
            "0 = Final response only\n1 = Tool names shown\n2 = Detailed tool info",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )


# --- Message handlers ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("You're not authorized to use this bot.")
        return

    if not check_rate_limit(user_id):
        await update.message.reply_text(f"Rate limit exceeded. Max {RATE_LIMIT_REQUESTS} messages per {RATE_LIMIT_WINDOW}s.")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    import base64
    b64_data = base64.b64encode(bytes(file_bytes)).decode()

    caption = update.message.caption or "Analyze this image."
    history = load_conversation(user_id)

    placeholder = await update.message.reply_text("🔍 Analyzing image...")
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(send_typing(update.effective_chat.id, context, stop_typing))

    try:
        if CLAUDE_MODE == "api":
            files = [{"type": "image", "media_type": "image/jpeg", "data": b64_data}]
            response, stats = await call_claude(caption, history, user_id, files=files)
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, dir=CLAUDE_CLI_WORK_DIR)
            tmp.write(bytes(file_bytes))
            tmp.close()
            files = [{"path": tmp.name, "name": "image.jpg"}]
            prompt = f"{caption}\n\n[Image saved at: {tmp.name}]"
            response, stats = await call_claude(prompt, history, user_id, files=files)
            os.unlink(tmp.name)

        save_message(user_id, "user", f"[Image] {caption}")
        save_message(user_id, "assistant", response)
        track_usage(user_id, stats.get("input_tokens", 0), stats.get("output_tokens", 0))

        verbose = get_verbose(user_id)
        if verbose >= 1 and stats.get("tools_used"):
            tools_str = ", ".join(stats["tools_used"][:5])
            response = f"🔧 _{tools_str}_\n\n{response}"

        await send_response(placeholder, update, response)
    except Exception as e:
        logger.exception("Error handling photo")
        await placeholder.edit_text(f"Error: {e}")
    finally:
        stop_typing.set()
        await typing_task


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("You're not authorized to use this bot.")
        return

    if not check_rate_limit(user_id):
        await update.message.reply_text(f"Rate limit exceeded. Max {RATE_LIMIT_REQUESTS} messages per {RATE_LIMIT_WINDOW}s.")
        return

    doc = update.message.document
    if doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("File too large (max 10MB).")
        return

    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()
    filename = doc.file_name or "file"
    caption = update.message.caption or f"Analyze this file: {filename}"

    history = load_conversation(user_id)
    placeholder = await update.message.reply_text(f"📄 Processing {filename}...")
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(send_typing(update.effective_chat.id, context, stop_typing))

    try:
        is_image = doc.mime_type and doc.mime_type.startswith("image/")

        if is_image and CLAUDE_MODE == "api":
            import base64
            b64_data = base64.b64encode(bytes(file_bytes)).decode()
            files = [{"type": "image", "media_type": doc.mime_type, "data": b64_data}]
            response, stats = await call_claude(caption, history, user_id, files=files)
        else:
            tmp = tempfile.NamedTemporaryFile(
                suffix=Path(filename).suffix or ".txt",
                delete=False,
                dir=CLAUDE_CLI_WORK_DIR,
                prefix="tg_upload_"
            )
            tmp.write(bytes(file_bytes))
            tmp.close()

            if CLAUDE_MODE == "api":
                try:
                    content = bytes(file_bytes).decode("utf-8")
                    files = [{"type": "text", "name": filename, "data": content}]
                except UnicodeDecodeError:
                    files = [{"type": "text", "name": filename, "data": f"[Binary file saved at {tmp.name}]"}]
                response, stats = await call_claude(caption, history, user_id, files=files)
            else:
                prompt = f"{caption}\n\n[File '{filename}' saved at: {tmp.name}]"
                files = [{"path": tmp.name, "name": filename}]
                response, stats = await call_claude(prompt, history, user_id, files=files)

            os.unlink(tmp.name)

        save_message(user_id, "user", f"[File: {filename}] {caption}")
        save_message(user_id, "assistant", response)
        track_usage(user_id, stats.get("input_tokens", 0), stats.get("output_tokens", 0))

        verbose = get_verbose(user_id)
        if verbose >= 1 and stats.get("tools_used"):
            tools_str = ", ".join(stats["tools_used"][:5])
            response = f"🔧 _{tools_str}_\n\n{response}"

        await send_response(placeholder, update, response)
    except Exception as e:
        logger.exception("Error handling document")
        await placeholder.edit_text(f"Error: {e}")
    finally:
        stop_typing.set()
        await typing_task


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await update.message.reply_text("You're not authorized to use this bot.")
        return

    if not check_rate_limit(user_id):
        await update.message.reply_text(f"Rate limit exceeded. Max {RATE_LIMIT_REQUESTS} messages per {RATE_LIMIT_WINDOW}s.")
        return

    user_text = update.message.text
    if not user_text:
        return

    history = load_conversation(user_id)
    placeholder = await update.message.reply_text("⏳ Thinking...")
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(send_typing(update.effective_chat.id, context, stop_typing))

    try:
        response, stats = await call_claude(user_text, history, user_id)

        save_message(user_id, "user", user_text)
        save_message(user_id, "assistant", response)
        track_usage(user_id, stats.get("input_tokens", 0), stats.get("output_tokens", 0))

        verbose = get_verbose(user_id)
        if verbose >= 1 and stats.get("tools_used"):
            tools_str = ", ".join(stats["tools_used"][:5])
            response = f"🔧 _{tools_str}_\n\n{response}"
        if verbose >= 2 and stats.get("input_tokens"):
            response += f"\n\n_({stats['input_tokens']}→{stats['output_tokens']} tokens)_"

        await send_response(placeholder, update, response)

    except Exception as e:
        logger.exception("Error handling message")
        await placeholder.edit_text(f"Error: {e}")
    finally:
        stop_typing.set()
        await typing_task


# --- Main ---

def main():
    init_db()

    if CLAUDE_MODE == "api":
        get_api_client()
        logger.info(f"Using API mode with model {CLAUDE_MODEL}")
    else:
        logger.info(f"Using CLI mode (bare, default model: {_default_cli_model}, timeout: {CLAUDE_TIMEOUT}s)")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("verbose", cmd_verbose))
    app.add_handler(CommandHandler("permissions", cmd_permissions))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(perm|model|verbose):"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

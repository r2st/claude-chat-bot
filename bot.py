import os
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

CLAUDE_MODE = os.environ.get("CLAUDE_MODE", "cli")  # "cli" or "api"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful assistant accessible via Telegram.")

CLAUDE_CLI_WORK_DIR = os.environ.get("CLAUDE_CLI_WORK_DIR", os.path.expanduser("~"))
CLAUDE_CLI_ADD_DIRS = os.environ.get("CLAUDE_CLI_ADD_DIRS", "")

ALLOWED_USER_IDS = set()
raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
if raw_ids:
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip()}

PERMISSION_MODES = {
    "default": "Default (prompts for everything)",
    "acceptEdits": "Accept Edits (auto-approve file read/write)",
    "auto": "Auto (auto-approve most actions)",
    "bypassPermissions": "Bypass All (no restrictions)",
}

conversations: dict[int, list[dict]] = {}
user_permission_mode: dict[int, str] = {}

_default_perm = os.environ.get("CLAUDE_CLI_PERMISSION_MODE", "")

# Lazy-loaded API client
_api_client = None


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


def get_permission_mode(user_id: int) -> str:
    return user_permission_mode.get(user_id, _default_perm)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode_label = "Claude CLI" if CLAUDE_MODE == "cli" else f"Claude API ({CLAUDE_MODEL})"
    await update.message.reply_text(
        f"Hi! I'm a Claude bot powered by {mode_label}.\n\n"
        "Send me any message and I'll respond.\n\n"
        "Commands:\n"
        "/reset - Clear conversation history\n"
        "/mode - Show current mode and model\n"
        "/permissions - Change CLI permission mode\n"
        "/id - Show your Telegram user ID"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your user ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    perm = get_permission_mode(update.effective_user.id) or "default"
    info = f"Mode: `{CLAUDE_MODE}`\nModel: `{CLAUDE_MODEL}`\nMax tokens: `{MAX_TOKENS}`\nPermissions: `{perm}`"
    await update.message.reply_text(info, parse_mode="Markdown")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations.pop(update.effective_user.id, None)
    await update.message.reply_text("Conversation cleared.")


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


async def handle_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("perm:"):
        return

    mode = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    if not is_allowed(user_id):
        return

    user_permission_mode[user_id] = mode if mode != "default" else ""
    label = PERMISSION_MODES.get(mode, mode)

    buttons = []
    current = get_permission_mode(user_id) or "default"
    for mode_key, mode_label in PERMISSION_MODES.items():
        marker = " ✓" if mode_key == current else ""
        buttons.append([InlineKeyboardButton(f"{mode_label}{marker}", callback_data=f"perm:{mode_key}")])

    await query.edit_message_text(
        f"Permission mode set to: *{label}*\n\nSelect a mode:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def call_claude_cli(prompt: str, history: list[dict], user_id: int) -> str:
    full_prompt = ""
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        full_prompt += f"{role}: {msg['content']}\n\n"
    full_prompt += f"User: {prompt}"

    cmd = ["claude", "-p", full_prompt]
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
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error = stderr.decode().strip()
        logger.error(f"Claude CLI error (exit {proc.returncode}): {error}")
        raise RuntimeError(f"CLI error: {error}")

    return stdout.decode().strip()


def call_claude_api(messages: list[dict]) -> str:
    client = get_api_client()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text


async def call_claude(prompt: str, history: list[dict], user_id: int) -> str:
    if CLAUDE_MODE == "api":
        messages = history + [{"role": "user", "content": prompt}]
        return call_claude_api(messages)
    return await call_claude_cli(prompt, history, user_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await update.message.reply_text("You're not authorized to use this bot.")
        return

    user_text = update.message.text
    if not user_text:
        return

    if user_id not in conversations:
        conversations[user_id] = []

    placeholder = await update.message.reply_text("Thinking...")

    try:
        response = await call_claude(user_text, conversations[user_id], user_id)

        conversations[user_id].append({"role": "user", "content": user_text})
        conversations[user_id].append({"role": "assistant", "content": response})

        if len(conversations[user_id]) > 20:
            conversations[user_id] = conversations[user_id][-20:]

        if len(response) <= 4096:
            await placeholder.edit_text(response)
        else:
            await placeholder.edit_text(response[:4096])
            for i in range(4096, len(response), 4096):
                await update.message.reply_text(response[i:i + 4096])

    except Exception as e:
        logger.exception("Error handling message")
        if user_id in conversations and conversations[user_id]:
            conversations[user_id].pop()
        await placeholder.edit_text(f"Error: {e}")


def main():
    if CLAUDE_MODE == "api":
        get_api_client()
        logger.info(f"Using API mode with model {CLAUDE_MODEL}")
    else:
        logger.info("Using CLI mode (claude -p)")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("permissions", cmd_permissions))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(handle_permission_callback, pattern=r"^perm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

# Telegram Claude Bot

A Telegram bot that connects to Claude AI. Supports two modes:

- **CLI mode** — Uses the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude -p`). No API key needed if you have a Claude subscription.
- **API mode** — Uses the [Anthropic API](https://docs.anthropic.com/en/api) directly. Requires an API key. Works in Docker.

## Quick Start

```bash
# Clone
git clone https://github.com/r2st/telegram-claude_bot.git
cd telegram-claude_bot

# Install
./scripts/install.sh

# Edit .env with your tokens
nano .env

# Run
./scripts/start.sh
```

## Setup

### 1. Create a Telegram Bot

1. Open Telegram and search for **@BotFather** (look for the verified blue checkmark)
2. Start a chat and send `/newbot`
3. **Choose a display name** — BotFather will ask: *"How are we going to call it?"*
   Enter any name, e.g. `My Claude Bot`
4. **Choose a username** — BotFather will ask: *"Now let's choose a username."*
   It must end in `bot`, e.g. `my_claude_helper_bot`
5. BotFather will reply with your **HTTP API token** — it looks like:
   ```
   123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
   ```
6. Copy this token — you'll need it for the `TELEGRAM_BOT_TOKEN` variable in `.env`

**Optional: customize your bot**

After creation, you can send these commands to BotFather:

| Command | What it does |
|---|---|
| `/setdescription` | Set the text users see before starting the bot |
| `/setabouttext` | Set the bio shown on the bot's profile |
| `/setuserpic` | Upload a profile picture for the bot |
| `/setcommands` | Register command hints (see below) |

To register command hints so users see autocomplete in the chat:

```
/setcommands
```

Select your bot, then send:

```
start - Welcome message
reset - Clear conversation history
mode - Show current mode and model
id - Show your Telegram user ID
```

**Finding your user ID (for ALLOWED_USER_IDS)**

1. Start your bot and send `/id`
2. It will reply with your numeric user ID
3. Add it to `ALLOWED_USER_IDS` in `.env` to restrict access

### 2. Configure

Copy `.env.example` to `.env` and set your values:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from BotFather |
| `CLAUDE_MODE` | No | `cli` (default) or `api` |
| `ANTHROPIC_API_KEY` | API mode | API key from [console.anthropic.com](https://console.anthropic.com) |
| `CLAUDE_MODEL` | No | Model name (default: `claude-sonnet-4-20250514`) |
| `MAX_TOKENS` | No | Max response tokens (default: `4096`) |
| `SYSTEM_PROMPT` | No | Custom system prompt |
| `CLAUDE_CLI_WORK_DIR` | No | Working directory for CLI mode (default: `~`) |
| `CLAUDE_CLI_ADD_DIRS` | No | Extra directories the CLI can access (comma-separated) |
| `CLAUDE_CLI_PERMISSION_MODE` | No | Permission mode: `auto`, `acceptEdits`, or `bypassPermissions` |
| `CLAUDE_CLI_MODEL` | No | Default CLI model: `haiku`, `sonnet` (default), or `opus` |
| `CLAUDE_TIMEOUT` | No | CLI response timeout in seconds (default: `120`) |
| `ALLOWED_USER_IDS` | No | Comma-separated user IDs to restrict access |

### 3. Choose a mode

**CLI mode** (default) — requires Claude Code CLI installed and authenticated:

```bash
# Install Claude Code CLI: https://docs.anthropic.com/en/docs/claude-code
# Then authenticate:
claude auth login
```

By default, the CLI runs from your home directory. To access other directories and skip permission prompts:

```bash
# In .env:
CLAUDE_CLI_WORK_DIR=/Users/you
CLAUDE_CLI_ADD_DIRS=/Users/you/projects,/Users/you/documents
CLAUDE_CLI_PERMISSION_MODE=auto
```

Permission modes:

| Mode | Behavior |
|---|---|
| *(empty)* | Default — prompts for each tool use (blocks in non-interactive `-p` mode) |
| `acceptEdits` | Auto-approves file reads/writes, prompts for shell commands |
| `auto` | Auto-approves most actions |
| `bypassPermissions` | Approves everything (use with caution) |

**API mode** — requires an Anthropic API key:

```bash
# In .env:
CLAUDE_MODE=api
ANTHROPIC_API_KEY=sk-ant-...
```

## Running

### Foreground

```bash
./scripts/start.sh
```

### As a system service (macOS launchd / Linux systemd)

```bash
./scripts/service.sh install    # Install and start
./scripts/service.sh status     # Check status
./scripts/service.sh logs       # Tail logs
./scripts/service.sh restart    # Restart
./scripts/service.sh stop       # Stop
./scripts/service.sh uninstall  # Remove service
```

### Docker (API mode only)

```bash
# Set CLAUDE_MODE=api in .env
docker compose up -d
docker logs -f claude-telegram-bot
```

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/reset` | Clear conversation history |
| `/mode` | Show current mode and model |
| `/model` | Switch CLI model (haiku/sonnet/opus) |
| `/permissions` | Change CLI permission mode |
| `/id` | Show your Telegram user ID |

## Security

- Set `ALLOWED_USER_IDS` to restrict who can use the bot
- Never commit `.env` — it's in `.gitignore`
- In CLI mode, the bot inherits your Claude auth — don't run on untrusted machines

## License

MIT

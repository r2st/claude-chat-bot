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

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow the prompts
3. Copy the bot token

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
| `ALLOWED_USER_IDS` | No | Comma-separated user IDs to restrict access |

### 3. Choose a mode

**CLI mode** (default) — requires Claude Code CLI installed and authenticated:

```bash
# Install Claude Code CLI: https://docs.anthropic.com/en/docs/claude-code
# Then authenticate:
claude auth login
```

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
| `/id` | Show your Telegram user ID |

## Security

- Set `ALLOWED_USER_IDS` to restrict who can use the bot
- Never commit `.env` — it's in `.gitignore`
- In CLI mode, the bot inherits your Claude auth — don't run on untrusted machines

## License

MIT

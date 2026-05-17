# telechat

> Claude AI on your phone/desktop — personal, self-hosted messenger bot for Telegram, WhatsApp, and Slack.

## Quick Start

```bash
npx telechat
```

First run automatically installs dependencies, checks your environment, and runs the setup wizard. After that, it starts the bot as a background service.

## Requirements

- **Node.js 16+** (for the CLI)
- **Python 3.9+** (auto-detected)
- **Claude CLI** (optional, for free CLI mode — `npm install -g @anthropic-ai/claude-code`)

## Commands

```bash
telechat                # Start bot as background service
telechat stop           # Stop the bot
telechat restart        # Restart the bot
telechat status         # Check if running
telechat logs           # Tail the bot log
telechat env            # Show environment variables (tokens masked)
telechat env clean      # Remove .env file (clear all credentials)
telechat clean          # Same as env clean
telechat init           # AI-guided setup using Claude CLI
telechat setup          # Manual setup wizard
telechat update         # Update to latest version
telechat --debug        # Start with verbose logging
telechat --version      # Show version
telechat --help         # Show all commands
```

## Setup Options

### `telechat init` (Recommended)

AI-guided setup powered by Claude CLI. Opens the right pages in your browser, walks you through each platform step by step, validates credentials, and writes `.env` automatically.

- Detects already-configured platforms and asks to keep or reconfigure
- Opens Telegram Web for QR login, then navigates to @BotFather and @userinfobot
- Opens Green API console for WhatsApp credentials
- Opens Slack API console with detailed setup instructions
- Validates every token via API before saving

Requires Claude CLI: `npm install -g @anthropic-ai/claude-code && claude auth login`

### `telechat setup`

Manual setup wizard with step-by-step prompts. Works without Claude CLI.

## How It Works

- Bot runs as a **background service** — survives terminal close and Ctrl+C
- Manage with `telechat start/stop/restart/status`
- Logs go to `bot.log` — view with `telechat logs`
- Config stored in `.env` — view with `telechat env`, edit and `telechat restart`
- Conversation history stored in `bot.db` (SQLite)

## Python Users

```bash
pip install telechatai
python -m telechat_pkg.main
```

## Documentation

Full docs: https://github.com/telechatai/telechat

## License

MIT

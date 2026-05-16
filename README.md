# Claude Messenger Bot

> **Claude AI on your phone** — personal, self-hosted, zero-infrastructure.  
> Supports **WhatsApp** and **Telegram** simultaneously from a single process.

A bot that connects to Claude AI via two modes:

- **CLI mode** — Uses the Claude Code CLI (`claude`). No API key needed if you have a Claude subscription.
- **API mode** — Uses the Anthropic API directly. Requires an API key. Works in Docker.

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

## Platform comparison

| | Telegram | WhatsApp |
|--|----------|----------|
| Bridge | Telegram Bot API (free) | [Green API](https://green-api.com) free tier |
| Setup | Talk to @BotFather | Scan a QR code |
| Photo / file support | Yes | Text only (currently) |
| Inline buttons | Yes (`/model`, `/permissions`, …) | No |
| Works without public URL | Yes (polling) | Yes (polling) |

---

## Setup

### 1 — Choose your platform

Set `BOT_MODE` in `.env`:

| Value | What starts |
|-------|-------------|
| `telegram` | Telegram only *(default)* |
| `whatsapp` | WhatsApp only |
| `both` | Both simultaneously (one process) |

---

### 2a — Telegram setup

1. Open Telegram and search for **@BotFather** (verified blue checkmark).
2. Send `/newbot` and follow the prompts.
3. Copy the token → set `TELEGRAM_BOT_TOKEN` in `.env`.

**Optional: customize your bot**

| BotFather command | What it does |
|-------------------|--------------|
| `/setdescription` | Text users see before starting the bot |
| `/setabouttext` | Bio shown on the bot's profile |
| `/setuserpic` | Profile picture |
| `/setcommands` | Register autocomplete hints |

Register command hints:
```
start - Welcome message
reset - Clear conversation history
mode - Show current mode and model
id - Show your Telegram user ID
```

**Finding your user ID (for `ALLOWED_USER_IDS`)**

1. Start your bot and send `/id`
2. Copy the numeric ID → paste into `ALLOWED_USER_IDS` in `.env`

---

### 2b — WhatsApp setup (Green API — free, no Meta account needed)

1. Sign up at <https://console.green-api.com>
2. Click **Create instance** → choose **Developer** plan (free — 1 500 msg/month)
3. In the instance dashboard → **Scan QR** → scan with your WhatsApp phone
4. Copy **Instance ID** and **API Token** → paste into `.env`:

```env
BOT_MODE=whatsapp
GREEN_API_INSTANCE_ID=1234567890
GREEN_API_TOKEN=your_token_here
WHATSAPP_ALLOWED_NUMBERS=919876543210   # your number without the +
```

> **Corporate network note:** Green API works over standard HTTPS polling — no
> webhook or public URL needed. If Telegram is blocked on your network, use
> `BOT_MODE=whatsapp` instead.

---

### 3 — Configure Claude

All Claude settings apply to both platforms:

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODE` | `cli` | `cli` or `api` |
| `ANTHROPIC_API_KEY` | — | Required for API mode |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | API mode model |
| `SYSTEM_PROMPT` | _(generic)_ | Your personal instructions to Claude |
| `CLAUDE_CLI_WORK_DIR` | `~` | Working directory for CLI |
| `CLAUDE_CLI_ADD_DIRS` | — | Comma-separated extra dirs Claude can access |
| `CLAUDE_CLI_PERMISSION_MODE` | — | `acceptEdits` / `auto` / `bypassPermissions` |
| `CLAUDE_CLI_MODEL` | `sonnet` | CLI model: `haiku` / `sonnet` / `opus` |
| `CLAUDE_TIMEOUT` | `180` | Seconds to wait for Claude |
| `RATE_LIMIT_REQUESTS` | `20` | Max messages per window |
| `RATE_LIMIT_WINDOW` | `60` | Rate limit window (seconds) |

**CLI mode** — requires Claude Code CLI installed and authenticated:

```bash
# Install Claude Code CLI: https://docs.anthropic.com/en/docs/claude-code
claude auth login
```

**API mode:**

```env
CLAUDE_MODE=api
ANTHROPIC_API_KEY=sk-ant-...
```

---

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

---

## Telegram commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/reset` | Clear conversation history |
| `/mode` | Show current mode and model |
| `/model` | Switch model (haiku / sonnet / opus) |
| `/verbose` | Set output verbosity: 0 quiet · 1 normal · 2 detailed |
| `/permissions` | Change CLI permission mode |
| `/usage` | Show usage statistics |
| `/id` | Show your Telegram user ID |

---

## WhatsApp usage

Just send a message. There are no slash commands — WhatsApp is intentionally
kept simple.

---

## Project structure

```
├── main.py            Entry point — reads BOT_MODE, starts adapters
├── claude_core.py     Shared: Claude CLI/API, SQLite history, rate limiting
├── telegram_bot.py    Telegram adapter
├── whatsapp_bot.py    WhatsApp adapter (Green API polling)
├── bot.py             Backward-compat shim (runs Telegram, same as before)
├── scripts/
│   ├── install.sh
│   ├── start.sh
│   └── service.sh
├── Dockerfile         (API mode only)
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Features

- **Dual platform** — Telegram and WhatsApp from one process (`BOT_MODE=both`)
- **Dual Claude mode** — CLI (free with Claude subscription) or API
- **Typing indicator** — shows "typing…" while Claude processes
- **Image & file analysis** — Telegram only (photos + documents)
- **Model switching** — haiku / sonnet / opus from Telegram inline buttons
- **Verbose mode** — see what tools Claude is using
- **Rate limiting** — configurable per-user throttling
- **Persistent history** — SQLite, keyed per platform+user; survives restarts
- **Usage tracking** — per-user message and token statistics
- **Markdown rendering** — formatted responses with plain-text fallback

---

## Per-developer sharing

Each developer runs their **own instance** on their own machine:

1. Clone this repo
2. `./scripts/install.sh`
3. Set their own credentials + `SYSTEM_PROMPT` in `.env`
4. `./scripts/start.sh`

No shared server. No shared credentials. Fully private.

---

## Security

- Set `ALLOWED_USER_IDS` (Telegram) or `WHATSAPP_ALLOWED_NUMBERS` to restrict who can use your bot
- Never commit `.env` — it is in `.gitignore`
- In CLI mode the bot inherits your Claude auth — don't run on untrusted machines

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| WhatsApp: no replies | Check instance status in Green API console — must be `authorized` |
| Telegram: SSL/handshake error | Telegram may be blocked on your network; use `BOT_MODE=whatsapp` |
| `claude: command not found` | Install Claude Code CLI and ensure it's in PATH |
| Response cut off | Bot auto-chunks at 4 000 chars per message — expected |
| Bot stops after reboot | Use `./scripts/service.sh install` for a proper system service |

---

## License

MIT

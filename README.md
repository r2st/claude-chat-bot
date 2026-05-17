# Telechat

> **Claude AI on your phone / desktop** — personal, self-hosted, zero-infrastructure.  
> Supports **WhatsApp**, **Telegram**, and **Slack** simultaneously from a single process.

A bot that connects to Claude AI via two modes:

- **CLI mode** — Uses the Claude Code CLI (`claude`). No API key needed if you have a Claude subscription.
- **API mode** — Uses the Anthropic API directly. Requires an API key. Works in Docker.

## Install

```bash
# npm (recommended — handles everything)
npx telechat

# pip
pip install telechatai
python -m telechat_pkg.main
```

## Quick Start

### Option A: AI-guided setup (recommended)

```bash
npx telechat init
```

Claude CLI walks you through each platform interactively:
- Opens Telegram Web for QR login → navigates to @BotFather and @userinfobot
- Opens Green API console for WhatsApp credentials
- Opens Slack API console with step-by-step instructions
- Validates every token before saving
- Detects existing config and asks to keep or reconfigure

Requires: `npm install -g @anthropic-ai/claude-code && claude auth login`

### Option B: Manual setup

```bash
npx telechat setup
```

Step-by-step wizard with prompts. Works without Claude CLI.

### Option C: From source

```bash
git clone https://github.com/telechatai/telechat.git
cd telechat
./scripts/install.sh
nano .env
./scripts/start.sh
```

## Commands

```bash
telechat              # Start bot as background service
telechat stop         # Stop the bot
telechat restart      # Restart the bot
telechat status       # Check if running
telechat logs         # Tail the bot log
telechat env          # Show environment variables (tokens masked)
telechat env clean    # Remove .env file (clear all credentials)
telechat clean        # Same as env clean
telechat init         # AI-guided setup using Claude CLI
telechat setup        # Manual setup wizard
telechat update       # Update to latest version
telechat --debug      # Start with verbose logging
telechat --version    # Show version
telechat --help       # Show all commands
```

The bot runs as a **background service** — it survives terminal close and Ctrl+C. Manage it with `start/stop/restart/status`.

---

## Platform comparison

| | Telegram | WhatsApp | Slack |
|--|----------|----------|-------|
| Bridge | Telegram Bot API | [Green API](https://green-api.com) free tier | Slack Bolt + Socket Mode |
| Setup | Talk to @BotFather | Scan a QR code | Create a Slack app |
| Photo / file support | Yes | Text only | Text only |
| Interactive UI | Inline buttons | No | Reactions as status indicator |
| Works without public URL | Yes (polling) | Yes (polling) | Yes (WebSocket) |
| Works on corporate Wi-Fi | Depends | Yes | Yes |

---

## Setup

### 1 — Choose your platform(s)

Set `BOT_MODE` in `.env` — accepts a comma-separated list or a shorthand:

| Value | What starts |
|-------|-------------|
| `telegram` | Telegram only *(default)* |
| `whatsapp` | WhatsApp only |
| `slack` | Slack only |
| `telegram,slack` | Telegram + Slack |
| `telegram,whatsapp` | Telegram + WhatsApp |
| `both` | Telegram + WhatsApp (legacy alias) |
| `all` | All three platforms |

---

### 2a — Telegram setup

1. Open **Telegram Web**: https://web.telegram.org/k/
2. Log in by scanning the QR code:
   - Open Telegram on your phone
   - Go to **Settings → Devices → Link Desktop Device**
   - Point your phone camera at the QR code
3. Search for **@BotFather** and send `/newbot`
4. Pick a display name and a username (must end in `bot`)
5. Copy the token → set `TELEGRAM_BOT_TOKEN` in `.env`

**Finding your user ID (for access control)**

1. Search for **@userinfobot** in Telegram Web
2. Send any message — it replies with your numeric ID
3. Copy the ID → set `TELEGRAM_ALLOWED_USER_IDS` in `.env`

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

---

### 2b — Slack setup (Socket Mode — no public URL needed)

> **Corporate workspace?** Most company Slack workspaces block individual users from installing apps. Create a **free personal workspace** at [slack.com/get-started](https://slack.com/get-started) instead.

#### Step-by-step

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
   - Enter a name (e.g. "TeleChat") and select your workspace

2. **Socket Mode** → toggle ON
   - Create an App-Level Token: name it `telechat`, add scope `connections:write`
   - Copy the `xapp-...` token → this is `SLACK_APP_TOKEN`

3. **OAuth & Permissions** → scroll to **Bot Token Scopes** → add:

   | Scope | Purpose |
   |-------|---------|
   | `chat:write` | Send messages |
   | `channels:history` | Read public channel messages |
   | `groups:history` | Read private channel messages |
   | `im:history` | Read DMs |
   | `im:write` | Open DM conversations |
   | `app_mentions:read` | Detect @mentions |
   | `reactions:write` | Show ⏳ while Claude thinks |

4. **Event Subscriptions** → toggle ON → Subscribe to bot events:
   `message.im`, `message.channels`, `message.groups`, `app_mention` → **Save Changes**

5. **Install App** → **Install to Workspace** → Allow
   - Copy the **Bot User OAuth Token** (`xoxb-...`) → this is `SLACK_BOT_TOKEN`
   - ⚠ **NOT** the User OAuth Token (`xoxp-`/`xoxe-`) — that won't work

6. Find your Slack member ID: click your profile pic → **Profile** → **⋮** → **Copy member ID**

#### `.env` for Slack

```env
BOT_MODE=slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=U01234567
```

#### How it works

| Trigger | How to use |
|---------|-----------|
| Direct message | Just message the bot |
| Channel | `@yourbot <question>` |
| Thread | Reply mentioning the bot to keep conversation in-thread |

A ⏳ reaction appears on your message while Claude is thinking, removed when done.

---

### 2c — WhatsApp setup (Green API — free, no Meta account needed)

1. Sign up at https://console.green-api.com (free Developer plan)
2. You'll see a free instance on the dashboard
3. Find **idInstance** (a number) and **apiTokenInstance** (a long hex string) at the top of your instance
4. Link your WhatsApp phone:
   - Click your instance → look for the QR code section
   - On your phone: **WhatsApp → Settings → Linked Devices → Link a Device**
   - Scan the QR code with your phone camera
5. Copy credentials into `.env`:

```env
BOT_MODE=whatsapp
GREEN_API_INSTANCE_ID=1234567890
GREEN_API_TOKEN=your_token_here
WHATSAPP_ALLOWED_NUMBERS=919876543210   # your number without the +
```

> **Corporate network note:** Green API works over standard HTTPS polling — no webhook or public URL needed.

---

### 3 — Configure Claude

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
npm install -g @anthropic-ai/claude-code
claude auth login
```

**API mode:**

```env
CLAUDE_MODE=api
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Running

The bot runs as a background service by default:

```bash
telechat              # Start
telechat stop         # Stop
telechat restart      # Restart
telechat status       # Check status
telechat logs         # Tail logs
telechat --debug      # Start with verbose logging
```

### From source

```bash
./scripts/start.sh                  # Foreground
./scripts/service.sh install        # macOS launchd / Linux systemd
```

### Docker (API mode only)

```bash
docker compose up -d
docker logs -f telechat
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

Just send a message. There are no slash commands — WhatsApp is intentionally kept simple.

---

## Project structure

```
├── main.py            Entry point — reads BOT_MODE, starts adapters
├── claude_core.py     Shared: Claude CLI/API, SQLite history, rate limiting
├── telegram_bot.py    Telegram adapter
├── whatsapp_bot.py    WhatsApp adapter (Green API polling)
├── slack_bot.py       Slack adapter (Socket Mode)
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

- **Three platforms** — Telegram, WhatsApp, and Slack from one process (`BOT_MODE=all`)
- **Background service** — runs detached, survives terminal close
- **AI-guided setup** — `telechat init` uses Claude CLI for interactive configuration
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

## Security

- Set `TELEGRAM_ALLOWED_USER_IDS`, `WHATSAPP_ALLOWED_NUMBERS`, or `SLACK_ALLOWED_USER_IDS` to restrict access
- View credentials with `telechat env` (tokens are masked)
- Clear all credentials with `telechat clean`
- Never commit `.env` — it is in `.gitignore`
- In CLI mode the bot inherits your Claude auth — don't run on untrusted machines

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `telechat: command not found` | Use `npx telechat` or run `npm install -g telechat` |
| Bot not responding | Check `telechat status` and `telechat logs` |
| Telegram 409 conflict | Another instance is running — `telechat stop` then `telechat start` |
| WhatsApp: no replies | Check instance status in Green API console — must be `authorized` |
| Slack: "error creating request" | Corporate workspace blocks installs — use a free personal workspace |
| Slack: bot doesn't respond | Check Socket Mode is enabled; App-Level Token needs `connections:write` |
| Slack: wrong token type | Use **Bot User OAuth Token** (`xoxb-...`), not User OAuth Token (`xoxp-`/`xoxe-`) |
| Slack: works in channels not DMs | Add `im:history` + `im:write` scopes and reinstall to workspace |
| `claude: command not found` | Install Claude Code CLI: `npm i -g @anthropic-ai/claude-code` |
| Response cut off | Bot auto-chunks at 4 000 chars per message — expected |
| Bot stops after reboot | Use `./scripts/service.sh install` for a system service |

---

## License

MIT

# telechat

> Claude AI on your phone/desktop — personal, self-hosted messenger bot for Telegram, WhatsApp, and Slack.

## Quick Start

One command — installs, checks your environment, fixes issues, and runs setup:

```bash
npx telechat
```

That's it. The wizard handles everything:
1. Checks Python, installs dependencies, fixes PATH issues
2. Platform selection (Telegram, WhatsApp, Slack, or any combination)
3. Token/credential setup with validation
4. Claude mode selection (CLI = free, API = pay-per-token)
5. Starts the bot

## Requirements

- **Node.js 16+** (for the CLI)
- **Python 3.9+** (auto-detected)
- **Claude CLI** (optional, for CLI mode — `npm install -g @anthropic-ai/claude-code`)

## Commands

```bash
npx telechat          # Install + setup + start (first time)
telechat              # Start bot (runs setup if no .env)
telechat setup        # Re-run setup wizard
telechat update       # Update to latest version
telechat --help       # Show all options
```

## Python users

```bash
pip install telechatai
telechat
```

## Documentation

Full docs: https://github.com/telechatai/telechat

## License

MIT

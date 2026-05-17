# telechat

> Claude AI on your phone/desktop — personal, self-hosted messenger bot for Telegram, WhatsApp, and Slack.

## Quick Start

One command to install, configure, and start:

```bash
npm install -g telechat
telechat
```

The interactive wizard walks you through:
1. Platform selection (Telegram, WhatsApp, Slack, or any combination)
2. Token/credential setup with step-by-step instructions
3. Claude mode selection (CLI with subscription or API with key)
4. Automatic Python package installation
5. Bot startup

## Requirements

- **Node.js 16+** (for the CLI)
- **Python 3.9+** (auto-detected)
- **Claude CLI** (optional, for CLI mode — `npm install -g @anthropic-ai/claude-code`)

## Commands

```bash
telechat              # Start bot (runs setup wizard if first time)
telechat setup        # Re-run setup wizard
telechat start        # Start without setup
telechat --version    # Show version
telechat --help       # Show help
```

## Python users

```bash
pip install telechat
telechat
```

## Documentation

Full docs: https://github.com/telechatai/telechat

## License

MIT

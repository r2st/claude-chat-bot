# telechat

> Claude AI on your phone/desktop — personal, self-hosted messenger bot for Telegram, WhatsApp, and Slack.

## Install

```bash
npm install -g telechat
```

Or use directly:

```bash
npx telechat
```

## Requirements

- **Python 3.9+** must be installed on your system
- A `.env` file with your bot tokens (Telegram, WhatsApp, and/or Slack)

The npm package is a thin wrapper that auto-installs the Python package from PyPI on first run.

## Usage

```bash
# Create a project directory
mkdir my-telechat && cd my-telechat

# Create .env with your config (see docs for all options)
cat > .env << 'EOF'
BOT_MODE=telegram
TELEGRAM_BOT_TOKEN=your_token_here
CLAUDE_MODE=cli
EOF

# Start the bot
telechat
```

## Python users

If you prefer pip:

```bash
pip install telechat
telechat
```

## Documentation

Full setup guide: https://github.com/telechatai/telechat

## License

MIT

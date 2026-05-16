#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_LABEL="com.claude.telegram-bot"

# --- macOS launchd ---

PLIST_PATH="$HOME/Library/LaunchAgents/${SERVICE_LABEL}.plist"

generate_plist() {
    local python_bin
    if [ -d "$PROJECT_DIR/venv" ]; then
        python_bin="$PROJECT_DIR/venv/bin/python"
    else
        python_bin="$(which python3)"
    fi

    # Read env vars from .env
    local env_entries=""
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        value="${value%\"}"
        value="${value#\"}"
        env_entries+="        <key>$key</key>
        <string>$value</string>
"
    done < "$PROJECT_DIR/.env"

    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SERVICE_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${python_bin}</string>
        <string>${PROJECT_DIR}/bot.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
${env_entries}        <key>PATH</key>
        <string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/bot.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/bot.log</string>
</dict>
</plist>
PLIST
}

# --- Linux systemd ---

SYSTEMD_PATH="$HOME/.config/systemd/user/${SERVICE_LABEL}.service"

generate_systemd() {
    local python_bin
    if [ -d "$PROJECT_DIR/venv" ]; then
        python_bin="$PROJECT_DIR/venv/bin/python"
    else
        python_bin="$(which python3)"
    fi

    mkdir -p "$(dirname "$SYSTEMD_PATH")"
    cat > "$SYSTEMD_PATH" <<UNIT
[Unit]
Description=Claude Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=${python_bin} ${PROJECT_DIR}/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
}

usage() {
    echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs}"
    exit 1
}

is_macos() { [[ "$(uname)" == "Darwin" ]]; }

cmd_install() {
    if is_macos; then
        generate_plist
        launchctl load "$PLIST_PATH"
        echo "Installed and started launchd service: $SERVICE_LABEL"
        echo "Logs: $PROJECT_DIR/bot.log"
    else
        generate_systemd
        systemctl --user daemon-reload
        systemctl --user enable --now "$SERVICE_LABEL"
        echo "Installed and started systemd service: $SERVICE_LABEL"
        echo "Logs: journalctl --user -u $SERVICE_LABEL -f"
    fi
}

cmd_uninstall() {
    if is_macos; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        echo "Service removed."
    else
        systemctl --user disable --now "$SERVICE_LABEL" 2>/dev/null || true
        rm -f "$SYSTEMD_PATH"
        systemctl --user daemon-reload
        echo "Service removed."
    fi
}

cmd_start() {
    if is_macos; then
        launchctl load "$PLIST_PATH" 2>/dev/null || launchctl kickstart "gui/$(id -u)/$SERVICE_LABEL"
    else
        systemctl --user start "$SERVICE_LABEL"
    fi
    echo "Started."
}

cmd_stop() {
    if is_macos; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
    else
        systemctl --user stop "$SERVICE_LABEL"
    fi
    echo "Stopped."
}

cmd_restart() {
    if is_macos; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        launchctl load "$PLIST_PATH"
    else
        systemctl --user restart "$SERVICE_LABEL"
    fi
    echo "Restarted."
}

cmd_status() {
    if is_macos; then
        launchctl list | grep "$SERVICE_LABEL" || echo "Not running."
    else
        systemctl --user status "$SERVICE_LABEL"
    fi
}

cmd_logs() {
    if is_macos; then
        tail -f "$PROJECT_DIR/bot.log"
    else
        journalctl --user -u "$SERVICE_LABEL" -f
    fi
}

case "${1:-}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    *)         usage ;;
esac

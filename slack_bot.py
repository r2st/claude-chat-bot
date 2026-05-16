"""
Slack adapter — uses Socket Mode (no webhook, no public URL).

Each developer creates their own Slack app and runs this locally.
Works on corporate networks — pure outbound WebSocket.

Setup (one-time, ~5 min):
  1. Go to https://api.slack.com/apps → Create New App → From scratch
  2. Settings → Socket Mode → Enable → Create App-Level Token
     Scopes: connections:write  →  copy the xapp-... token
  3. OAuth & Permissions → Bot Token Scopes:
       chat:write, channels:history, groups:history,
       im:history, im:write, app_mentions:read, reactions:write
  4. Event Subscriptions → Enable → Subscribe to bot events:
       message.im, message.channels, message.groups, app_mention
  5. Install to workspace → copy the xoxb-... Bot Token
  6. Invite your bot to any channels you want it active in (/invite @yourbot)
"""

import logging
import os
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import claude_core as cc

log = logging.getLogger(__name__)

PLATFORM = "slack"

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]   # xoxb-...
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]    # xapp-...

ALLOWED_SLACK_USERS: list[str] = [
    u.strip()
    for u in os.getenv("SLACK_ALLOWED_USER_IDS", "").split(",")
    if u.strip()
]

# ─── Slack app ─────────────────────────────────────────────────────────────────

app = App(token=SLACK_BOT_TOKEN)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def _allowed(user_id: str) -> bool:
    return not ALLOWED_SLACK_USERS or user_id in ALLOWED_SLACK_USERS


def _add_reaction(client, channel: str, ts: str, emoji: str) -> None:
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=emoji)
    except Exception:
        pass


def _remove_reaction(client, channel: str, ts: str, emoji: str) -> None:
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name=emoji)
    except Exception:
        pass


def _post_reply(client, channel: str, thread_ts: str, text: str) -> None:
    """Send reply, chunked if needed. Always in-thread to avoid noise."""
    chunk = 3000   # Slack's soft limit for readable chunks
    for i in range(0, len(text), chunk):
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text[i : i + chunk],
            mrkdwn=True,
        )


def _handle(client, channel: str, user_id: str, thread_ts: str, text: str) -> None:
    """Runs in a background thread so the Slack ack isn't blocked."""
    if not cc.check_rate_limit(f"slack:{user_id}"):
        _post_reply(
            client, channel, thread_ts,
            f"Rate limit: max {cc.RATE_LIMIT_REQUESTS} messages per {cc.RATE_LIMIT_WINDOW}s.",
        )
        return

    # ⏳ reaction = "thinking" indicator
    _add_reaction(client, channel, thread_ts, "hourglass_flowing_sand")
    try:
        log.info("← Slack [%s/%s] %s", channel, user_id, text[:120])
        history = cc.load_history(PLATFORM, user_id)

        if cc.CLAUDE_MODE == "api":
            reply, stats = cc.ask_claude_api(text, history)
        else:
            reply, stats = cc.ask_claude_sync(text, history)

        cc.save_turn(PLATFORM, user_id, text, reply)
        cc.track_usage(PLATFORM, user_id, stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        log.info("→ Slack [%s/%s] %s", channel, user_id, reply[:120])

        _post_reply(client, channel, thread_ts, reply)
    except Exception as exc:
        log.exception("Slack handler error")
        _post_reply(client, channel, thread_ts, f"[Error] {exc}")
    finally:
        _remove_reaction(client, channel, thread_ts, "hourglass_flowing_sand")


# ─── Event handlers ────────────────────────────────────────────────────────────

def _dispatch(client, event: dict) -> None:
    """Common dispatch for all message/mention events."""
    user_id  = event.get("user", "")
    channel  = event.get("channel", "")
    text     = (event.get("text") or "").strip()
    ts       = event.get("ts", "")
    thread_ts = event.get("thread_ts") or ts   # reply in-thread if already threaded

    # Ignore bot's own messages
    if event.get("bot_id") or not user_id or not text:
        return

    if not _allowed(user_id):
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="Sorry, you're not on the allowed list.",
        )
        return

    # Strip @mention from text (app_mention events include it)
    import re
    text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    if not text:
        return

    threading.Thread(
        target=_handle,
        args=(client, channel, user_id, thread_ts, text),
        daemon=True,
    ).start()


@app.event("app_mention")
def handle_mention(client, event, say):
    """Respond when @mentioned in any channel."""
    _dispatch(client, event)


@app.event("message")
def handle_message(client, event, say):
    """Respond to DMs and messages in channels where the bot is active."""
    # Skip subtypes (edits, joins, etc.) — only handle plain new messages
    if event.get("subtype"):
        return
    _dispatch(client, event)


# ─── Entry point ─────────────────────────────────────────────────────────────────

def run_slack() -> None:
    cc.init_db()
    log.info("Slack bot starting (Socket Mode)…")
    log.info("Model: %s | Claude mode: %s", cc.CLAUDE_MODEL, cc.CLAUDE_MODE)
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()   # blocking

"""
WhatsApp adapter — polls Green API, hands messages to claude_core.
Run via main.py with BOT_MODE=whatsapp or BOT_MODE=both.

Each user runs their own instance against their own Green API credentials
(personal free-tier account, QR-scanned to their WhatsApp number).
"""

import logging
import os
import threading
import time
from typing import Optional

import requests
from dotenv import load_dotenv

import claude_core as cc

load_dotenv()

log = logging.getLogger(__name__)

PLATFORM = "whatsapp"

# ─── Green API config ───────────────────────────────────────────────────────────

INSTANCE_ID   = os.environ["GREEN_API_INSTANCE_ID"]
API_TOKEN     = os.environ["GREEN_API_TOKEN"]
BASE_URL      = os.getenv("GREEN_API_BASE_URL", f"https://api.green-api.com").rstrip("/") + f"/waInstance{INSTANCE_ID}"

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))

ALLOWED_NUMBERS: list[str] = [
    n.strip().lstrip("+")
    for n in os.getenv("WHATSAPP_ALLOWED_NUMBERS", "").split(",")
    if n.strip()
]

# ─── Per-chat concurrency guards ────────────────────────────────────────────────

_locks: dict[str, threading.Lock] = {}


def _lock_for(chat_id: str) -> threading.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = threading.Lock()
    return _locks[chat_id]


# ─── Green API helpers ──────────────────────────────────────────────────────────

def _api(method: str, path: str, **kwargs) -> Optional[dict]:
    url = f"{BASE_URL}/{path}/{API_TOKEN}"
    try:
        r = requests.request(method, url, timeout=30, **kwargs)
        r.raise_for_status()
        return r.json() if r.text else {}
    except requests.RequestException as exc:
        log.error("Green API %s %s → %s", method, path, exc)
        return None


def receive_notification() -> Optional[dict]:
    return _api("GET", "receiveNotification")


def delete_notification(receipt_id: int) -> None:
    _api("DELETE", f"deleteNotification/{receipt_id}")


def send_message(chat_id: str, text: str) -> None:
    _api("POST", "sendMessage", json={"chatId": chat_id, "message": text})


def send_typing(chat_id: str) -> None:
    _api("POST", "sendChatState", json={"chatId": chat_id, "chatState": "textMessage"})


# ─── Auth / rate limit ──────────────────────────────────────────────────────────

def _allowed(sender: str) -> bool:
    if not ALLOWED_NUMBERS:
        return True
    number = sender.split("@")[0]   # "14155552671@c.us" → "14155552671"
    return number in ALLOWED_NUMBERS


# ─── Message handling ───────────────────────────────────────────────────────────

def _handle(chat_id: str, sender: str, text: str) -> None:
    """Runs in a background thread per chat."""
    lock = _lock_for(chat_id)
    if not lock.acquire(blocking=False):
        send_message(chat_id, "⏳ Still processing your previous message…")
        return

    try:
        if not cc.check_rate_limit(f"wa:{sender}"):
            send_message(
                chat_id,
                f"Rate limit: max {cc.RATE_LIMIT_REQUESTS} messages per {cc.RATE_LIMIT_WINDOW}s.",
            )
            return

        send_typing(chat_id)
        log.info("← WA [%s] %s", chat_id, text[:120])

        history = cc.load_history(PLATFORM, sender)

        if cc.CLAUDE_MODE == "api":
            reply, stats = cc.ask_claude_api(text, history)
        else:
            reply, stats = cc.ask_claude_sync(text, history)

        cc.save_turn(PLATFORM, sender, text, reply)
        cc.track_usage(PLATFORM, sender, stats.get("input_tokens", 0), stats.get("output_tokens", 0))

        log.info("→ WA [%s] %s", chat_id, reply[:120])

        # Chunk long replies (WhatsApp limit ≈ 65 536 chars; use 4 000 to be safe)
        chunk = 4000
        for i in range(0, len(reply), chunk):
            send_message(chat_id, reply[i : i + chunk])
            if i + chunk < len(reply):
                time.sleep(0.5)
    except Exception as exc:
        log.exception("Error in _handle")
        send_message(chat_id, f"[Error] {exc}")
    finally:
        lock.release()


def _process(notification: dict) -> None:
    receipt_id = notification.get("receiptId")
    body = notification.get("body", {})

    if body.get("typeWebhook") != "incomingMessageReceived":
        delete_notification(receipt_id)
        return

    msg_data = body.get("messageData", {})
    if msg_data.get("typeMessage") != "textMessage":
        delete_notification(receipt_id)
        return

    sender_data = body.get("senderData", {})
    chat_id = sender_data.get("chatId", "")
    sender  = sender_data.get("sender", chat_id)
    text    = msg_data.get("textMessageData", {}).get("textMessage", "").strip()

    delete_notification(receipt_id)   # ACK before processing → no re-delivery

    if not text:
        return

    if not _allowed(sender):
        log.warning("Rejected message from %s", sender)
        send_message(chat_id, "Sorry, you're not on the allowed list.")
        return

    threading.Thread(target=_handle, args=(chat_id, sender, text), daemon=True).start()


# ─── Entry point ─────────────────────────────────────────────────────────────────

def run_whatsapp() -> None:
    cc.init_db()
    log.info("WhatsApp bot started (Green API instance %s). Polling every %.1fs…", INSTANCE_ID, POLL_INTERVAL)
    log.info("Model: %s | Mode: %s", cc.CLAUDE_MODEL, cc.CLAUDE_MODE)

    while True:
        try:
            notification = receive_notification()
            if notification:
                _process(notification)
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("WhatsApp bot shutting down.")
            break
        except Exception as exc:
            log.exception("Poll loop error: %s", exc)
            time.sleep(5)

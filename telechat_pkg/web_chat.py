"""
Web chat adapter — serves a browser-based chat UI backed by Claude CLI/API/SDK.

Runs an aiohttp server with:
- GET  /         → chat UI (single-page app)
- GET  /health   → JSON health status
- WS   /ws       → WebSocket for real-time chat

Set WEB_CHAT_PORT in .env (default: 8585).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

from . import claude_core as cc
from .memory import MemoryStore
from .text_chunking import chunk_text

log = logging.getLogger(__name__)

PLATFORM = "web"

WEB_PORT = int(os.getenv("WEB_CHAT_PORT", "8585"))
WEB_BIND = os.getenv("WEB_CHAT_BIND", "0.0.0.0")
WEB_AUTH_TOKEN = os.getenv("WEB_CHAT_TOKEN", "")

_memory = MemoryStore()
_active_ws: dict[str, web.WebSocketResponse] = {}

_DEFAULT_MODEL = os.getenv("CLAUDE_CLI_MODEL", cc.CLAUDE_MODEL)
_DEFAULT_PERM = os.getenv("CLAUDE_CLI_PERMISSION_MODE", cc.CLAUDE_PERM_MODE)

_HTML_PATH = Path(__file__).parent / "web_chat_ui.html"


def _get_user_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


async def _index_handler(request: web.Request) -> web.Response:
    html = _HTML_PATH.read_text()
    return web.Response(text=html, content_type="text/html")


async def _health_handler(request: web.Request) -> web.Response:
    from . import health
    h = health.get_health()
    h["web_clients"] = len(_active_ws)
    status = 200 if h["status"] == "healthy" else 503
    return web.json_response(h, status=status)


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
    await ws.prepare(request)

    client_id = secrets.token_hex(8)
    authenticated = not WEB_AUTH_TOKEN

    async def send_json(data: dict):
        if not ws.closed:
            await ws.send_json(data)

    await send_json({
        "type": "connected",
        "auth_required": not authenticated,
    })

    _active_ws[client_id] = ws
    log.info("Web client connected: %s (auth_required=%s)", client_id, not authenticated)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await send_json({"type": "error", "text": "Invalid JSON"})
                    continue

                msg_type = data.get("type", "")

                if msg_type == "auth":
                    token = data.get("token", "")
                    if not WEB_AUTH_TOKEN or hmac.compare_digest(token, WEB_AUTH_TOKEN):
                        authenticated = True
                        await send_json({"type": "auth_ok"})
                    else:
                        await send_json({"type": "auth_fail"})
                    continue

                if not authenticated:
                    await send_json({"type": "error", "text": "Not authenticated"})
                    continue

                if msg_type == "message":
                    text = (data.get("text") or "").strip()
                    if not text:
                        continue

                    user_id = _get_user_id(client_id)

                    if text.startswith("/"):
                        await _handle_command(ws, send_json, user_id, text)
                        continue

                    asyncio.create_task(
                        _handle_chat(ws, send_json, user_id, client_id, text)
                    )

                elif msg_type == "cancel":
                    pass

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        _active_ws.pop(client_id, None)
        log.info("Web client disconnected: %s", client_id)

    return ws


async def _handle_command(
    ws: web.WebSocketResponse,
    send_json,
    user_id: str,
    text: str,
):
    cmd = text.split()[0].lower()
    args = text[len(cmd):].strip()

    if cmd == "/clear":
        sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
        cc.clear_history(PLATFORM, user_id, session_name=sess.name)
        sess.claude_session_id = None
        await send_json({"type": "system", "text": "Conversation cleared."})

    elif cmd == "/new":
        cc._session_mgr.new_session(PLATFORM, user_id, args or None)
        await send_json({"type": "system", "text": f"New session started{': ' + args if args else ''}."})

    elif cmd == "/model":
        if args:
            await send_json({"type": "system", "text": f"Model set to: {args}"})
        else:
            await send_json({"type": "system", "text": f"Current model: {cc.CLAUDE_MODEL}"})

    elif cmd == "/help":
        await send_json({
            "type": "system",
            "text": (
                "**Commands:**\n"
                "- `/clear` — clear conversation history\n"
                "- `/new [name]` — start a new session\n"
                "- `/model [name]` — show/set model\n"
                "- `/help` — show this help"
            ),
        })
    else:
        await send_json({"type": "system", "text": f"Unknown command: {cmd}. Type /help."})


async def _handle_chat(
    ws: web.WebSocketResponse,
    send_json,
    user_id: str,
    client_id: str,
    text: str,
):
    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    history = cc.load_history(PLATFORM, user_id, session_name=sess.name)

    msg_id = secrets.token_hex(6)
    await send_json({"type": "thinking", "msg_id": msg_id})

    collected_text = []

    async def _on_progress(tool_name: str, detail: str = ""):
        await send_json({
            "type": "progress",
            "msg_id": msg_id,
            "tool": tool_name,
            "detail": detail,
        })

    async def _on_text(text_chunk: str):
        collected_text.append(text_chunk)
        await send_json({
            "type": "stream",
            "msg_id": msg_id,
            "text": text_chunk,
        })

    def _is_cancelled() -> bool:
        return ws.closed or client_id not in _active_ws

    try:
        engine = cc.CLAUDE_MODE

        if engine == "api":
            reply, stats = await cc.ask_claude_api_async(
                text, history,
                system=cc.CLAUDE_SYSTEM,
                on_text=_on_text,
                is_cancelled=_is_cancelled,
            )
        elif engine == "sdk":
            reply, stats = await cc.ask_claude_sdk(
                text, history,
                model=cc.CLAUDE_MODEL,
                system=cc.CLAUDE_SYSTEM,
                add_dirs=cc.CLAUDE_ADD_DIRS,
                timeout=cc.CLAUDE_TIMEOUT,
                on_progress=_on_progress,
                on_text=_on_text,
                is_cancelled=_is_cancelled,
            )
            if stats.get("session_id"):
                sess.claude_session_id = stats["session_id"]
                sess.touch()
        else:
            cli_sid = sess.claude_session_id if sess.cli_session_valid else ""
            reply, stats = await cc.ask_claude_async(
                text, history,
                model=cc.CLAUDE_MODEL,
                system=cc.CLAUDE_SYSTEM,
                add_dirs=cc.CLAUDE_ADD_DIRS,
                perm_mode=_DEFAULT_PERM,
                timeout=cc.CLAUDE_TIMEOUT,
                on_progress=_on_progress,
                on_text=_on_text,
                is_cancelled=_is_cancelled,
                platform=PLATFORM,
                user_id=user_id,
                resume_session_id=cli_sid or "",
            )
            if stats.get("session_id"):
                sess.claude_session_id = stats["session_id"]
                sess.touch()

        cc.save_turn(PLATFORM, user_id, "user", text, session_name=sess.name)
        cc.save_turn(PLATFORM, user_id, "assistant", reply, session_name=sess.name)

        if stats:
            cc.track_usage(
                PLATFORM, user_id,
                stats.get("input_tokens", 0),
                stats.get("output_tokens", 0),
            )
            if stats.get("cost_usd"):
                cc.track_cost(PLATFORM, user_id, stats["cost_usd"])

        if not collected_text:
            await send_json({
                "type": "reply",
                "msg_id": msg_id,
                "text": reply,
                "stats": {
                    "input_tokens": stats.get("input_tokens", 0),
                    "output_tokens": stats.get("output_tokens", 0),
                    "cost_usd": stats.get("cost_usd", 0),
                    "tools_used": stats.get("tools_used", []),
                },
            })
        else:
            await send_json({
                "type": "done",
                "msg_id": msg_id,
                "stats": {
                    "input_tokens": stats.get("input_tokens", 0),
                    "output_tokens": stats.get("output_tokens", 0),
                    "cost_usd": stats.get("cost_usd", 0),
                    "tools_used": stats.get("tools_used", []),
                },
            })

    except Exception as exc:
        log.exception("Web chat error for client %s", client_id)
        await send_json({
            "type": "error",
            "msg_id": msg_id,
            "text": f"Error: {exc}",
        })


def _create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _index_handler)
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/ws", _ws_handler)
    return app


async def run_web_chat() -> None:
    app = _create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, WEB_BIND, WEB_PORT)
    await site.start()
    log.info("Web chat running on http://%s:%d", WEB_BIND, WEB_PORT)
    print(f"  Web chat: http://localhost:{WEB_PORT}")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()


def run_web_chat_sync() -> None:
    asyncio.run(run_web_chat())

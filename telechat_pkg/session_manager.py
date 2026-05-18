"""
Session Resume/Fork (Feature 4) — resume previous conversations or fork them.

Inspired by Claude Agent SDK's session persistence where sessions can be
resumed later or forked to explore different approaches.

Usage:
    from telechat_pkg.session_manager import SessionBrowser
    browser = SessionBrowser()
    sessions = browser.list_sessions("telegram", "123", limit=10)
    browser.resume_session("telegram", "123", session_name="coding-project")
    browser.fork_session("telegram", "123", "coding-project", "coding-project-alt")
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os

log = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    name: str
    created_at: float
    last_active: float
    message_count: int
    total_cost: float
    claude_session_id: str | None
    is_active: bool
    preview: str = ""


@dataclass
class ForkResult:
    new_session_name: str
    messages_copied: int
    success: bool
    error: str = ""


class SessionBrowser:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path(__file__).parent / "bot.db")
        self._db_path = db_path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def list_sessions(
        self,
        platform: str,
        user_id: str,
        *,
        limit: int = 10,
        include_preview: bool = True,
    ) -> list[SessionInfo]:
        """List all sessions for a user, ordered by last activity."""
        conn = self._conn()
        rows = conn.execute(
            """SELECT
                session_name,
                MIN(timestamp) as created_at,
                MAX(timestamp) as last_active,
                COUNT(*) as message_count,
                COALESCE(SUM(
                    CASE WHEN cost_usd IS NOT NULL THEN cost_usd ELSE 0 END
                ), 0) as total_cost
               FROM history
               WHERE platform = ? AND user_id = ?
                 AND session_name IS NOT NULL
               GROUP BY session_name
               ORDER BY last_active DESC
               LIMIT ?""",
            (platform, user_id, limit),
        ).fetchall()

        sessions = []
        for r in rows:
            preview = ""
            if include_preview:
                prev_row = conn.execute(
                    """SELECT user_text FROM history
                       WHERE platform = ? AND user_id = ? AND session_name = ?
                       ORDER BY timestamp DESC LIMIT 1""",
                    (platform, user_id, r["session_name"]),
                ).fetchone()
                if prev_row:
                    preview = (prev_row["user_text"] or "")[:100]

            sessions.append(SessionInfo(
                name=r["session_name"],
                created_at=r["created_at"],
                last_active=r["last_active"],
                message_count=r["message_count"],
                total_cost=r["total_cost"],
                claude_session_id=None,
                is_active=False,
                preview=preview,
            ))
        return sessions

    def get_session_history(
        self,
        platform: str,
        user_id: str,
        session_name: str,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """Get conversation history for a specific session."""
        conn = self._conn()
        rows = conn.execute(
            """SELECT user_text, bot_reply, timestamp
               FROM history
               WHERE platform = ? AND user_id = ? AND session_name = ?
               ORDER BY timestamp ASC
               LIMIT ?""",
            (platform, user_id, session_name, limit),
        ).fetchall()
        result = []
        for r in rows:
            result.append({"role": "user", "content": r["user_text"]})
            result.append({"role": "assistant", "content": r["bot_reply"]})
        return result

    def fork_session(
        self,
        platform: str,
        user_id: str,
        source_session: str,
        new_session_name: str | None = None,
        *,
        max_messages: int = 50,
    ) -> ForkResult:
        """Fork (copy) a session's history into a new session."""
        if not new_session_name:
            new_session_name = f"{source_session}-fork-{int(time.time()) % 10000}"

        conn = self._conn()
        # Check source exists
        rows = conn.execute(
            """SELECT user_text, bot_reply, timestamp
               FROM history
               WHERE platform = ? AND user_id = ? AND session_name = ?
               ORDER BY timestamp ASC
               LIMIT ?""",
            (platform, user_id, source_session, max_messages),
        ).fetchall()

        if not rows:
            return ForkResult(new_session_name, 0, False, f"Session '{source_session}' not found or empty")

        # Copy messages with new session name
        copied = 0
        now = time.time()
        for r in rows:
            conn.execute(
                """INSERT INTO history (platform, user_id, user_text, bot_reply, timestamp, session_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (platform, user_id, r["user_text"], r["bot_reply"], now + copied * 0.001, new_session_name),
            )
            copied += 1
        conn.commit()

        return ForkResult(new_session_name, copied, True)

    def search_sessions(
        self,
        platform: str,
        user_id: str,
        query: str,
        *,
        limit: int = 5,
    ) -> list[SessionInfo]:
        """Search across sessions by conversation content."""
        conn = self._conn()
        rows = conn.execute(
            """SELECT DISTINCT session_name
               FROM history
               WHERE platform = ? AND user_id = ?
                 AND (user_text LIKE '%' || ? || '%' OR bot_reply LIKE '%' || ? || '%')
                 AND session_name IS NOT NULL
               LIMIT ?""",
            (platform, user_id, query, query, limit),
        ).fetchall()

        session_names = [r["session_name"] for r in rows]
        return [s for s in self.list_sessions(platform, user_id, limit=50) if s.name in session_names]

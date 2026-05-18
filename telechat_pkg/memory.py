"""
Persistent memory layer for telechat — ported from knol-local.

Stores user memories in the same SQLite database (bot.db) with FTS5
full-text search and BM25 ranking weighted by importance scores.

Each memory belongs to a platform + user_id, so Telegram/WhatsApp/Slack
users each have their own memory namespace.

Usage:
    from memory import MemoryStore
    mem = MemoryStore()              # uses bot.db
    mem.remember("telegram", "123", "User prefers dark mode", tags=["preference"])
    results = mem.recall("telegram", "123", "dark mode")
    mem.forget("telegram", "123", "<uuid>")
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Memory:
    id: str
    platform: str
    user_id: str
    content: str
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class SearchResult(Memory):
    score: float = 0.0


class MemoryStore:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path(__file__).parent / "bot.db")
        self._db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id          TEXT PRIMARY KEY,
                platform    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                content     TEXT NOT NULL,
                tags        TEXT,
                importance  REAL NOT NULL DEFAULT 0.5,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_user
                ON memories(platform, user_id);
            CREATE INDEX IF NOT EXISTS idx_memories_updated
                ON memories(updated_at DESC);
        """)

        # FTS5 with Porter stemming for better recall
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content,
                    tags,
                    tokenize = 'porter unicode61',
                    content = 'memories',
                    content_rowid = 'rowid'
                )
            """)
        except sqlite3.OperationalError:
            # FTS5 not available — fall back to LIKE search
            pass

        # Triggers to keep FTS in sync
        for trigger_sql in [
            """CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.rowid, new.content, COALESCE(new.tags, ''));
            END""",
            """CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                VALUES ('delete', old.rowid, old.content, COALESCE(old.tags, ''));
            END""",
            """CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                VALUES ('delete', old.rowid, old.content, COALESCE(old.tags, ''));
                INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.rowid, new.content, COALESCE(new.tags, ''));
            END""",
        ]:
            try:
                conn.execute(trigger_sql)
            except sqlite3.OperationalError:
                pass

        conn.commit()

    def _has_fts(self) -> bool:
        try:
            self._conn().execute("SELECT 1 FROM memories_fts LIMIT 0")
            return True
        except sqlite3.OperationalError:
            return False

    _MEMORY_FIELDS = frozenset(Memory.__dataclass_fields__)

    def _parse_row(self, row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"],
            platform=row["platform"],
            user_id=row["user_id"],
            content=row["content"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            importance=row["importance"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_search_result(self, row: sqlite3.Row, score: float) -> SearchResult:
        d = {k: row[k] for k in self._MEMORY_FIELDS if k != "tags"}
        d["tags"] = json.loads(row["tags"]) if row["tags"] else []
        d["score"] = score
        return SearchResult(**d)

    # ── CRUD ──────────────────────────────────────────────────────────────

    def remember(
        self,
        platform: str,
        user_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 0.5,
    ) -> Memory:
        now = time.time()
        mem = Memory(
            id=str(uuid.uuid4()),
            platform=platform,
            user_id=user_id,
            content=content.strip(),
            tags=tags or [],
            importance=max(0.0, min(1.0, importance)),
            created_at=now,
            updated_at=now,
        )
        self._conn().execute(
            """INSERT INTO memories (id, platform, user_id, content, tags, importance, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem.id, mem.platform, mem.user_id, mem.content,
             json.dumps(mem.tags) if mem.tags else None,
             mem.importance, mem.created_at, mem.updated_at),
        )
        self._conn().commit()
        return mem

    def recall(
        self,
        platform: str,
        user_id: str,
        query: str,
        *,
        limit: int = 10,
        tags: list[str] | None = None,
    ) -> list[SearchResult]:
        conn = self._conn()
        tag_clause = ""
        tag_params: list[str] = []
        if tags:
            placeholders = ",".join("?" for _ in tags)
            tag_clause = f"AND EXISTS (SELECT 1 FROM json_each(m.tags) WHERE value IN ({placeholders}))"
            tag_params = list(tags)

        query = query.strip()
        if not query:
            rows = conn.execute(
                f"""SELECT *, 0.0 as rank FROM memories m
                    WHERE platform = ? AND user_id = ?
                    {tag_clause}
                    ORDER BY importance DESC, updated_at DESC
                    LIMIT ?""",
                [platform, user_id, *tag_params, limit],
            ).fetchall()
            return [self._row_to_search_result(r, 0.0) for r in rows]

        # Try FTS5 search first
        if self._has_fts():
            fts_query = self._to_fts_query(query)
            try:
                rows = conn.execute(
                    f"""SELECT m.*, f.rank
                        FROM memories_fts f
                        JOIN memories m ON m.rowid = f.rowid
                        WHERE memories_fts MATCH ?
                          AND m.platform = ? AND m.user_id = ?
                          {tag_clause}
                        ORDER BY f.rank * (1.0 / m.importance)
                        LIMIT ?""",
                    [fts_query, platform, user_id, *tag_params, limit],
                ).fetchall()
                return [self._row_to_search_result(r, r["rank"]) for r in rows]
            except sqlite3.OperationalError:
                pass

        # Fallback: LIKE search
        rows = conn.execute(
            f"""SELECT *, 0.0 as rank FROM memories m
                WHERE platform = ? AND user_id = ?
                  AND content LIKE '%' || ? || '%'
                  {tag_clause}
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?""",
            [platform, user_id, query, *tag_params, limit],
        ).fetchall()
        return [self._row_to_search_result(r, 0.0) for r in rows]

    def forget(self, platform: str, user_id: str, memory_id: str) -> bool:
        result = self._conn().execute(
            "DELETE FROM memories WHERE id = ? AND platform = ? AND user_id = ?",
            (memory_id, platform, user_id),
        )
        self._conn().commit()
        return result.rowcount > 0

    def update(
        self,
        platform: str,
        user_id: str,
        memory_id: str,
        *,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
    ) -> Memory | None:
        existing = self._conn().execute(
            "SELECT * FROM memories WHERE id = ? AND platform = ? AND user_id = ?",
            (memory_id, platform, user_id),
        ).fetchone()
        if not existing:
            return None

        new_content = content if content is not None else existing["content"]
        new_tags = tags if tags is not None else (json.loads(existing["tags"]) if existing["tags"] else [])
        new_importance = max(0.0, min(1.0, importance)) if importance is not None else existing["importance"]

        self._conn().execute(
            """UPDATE memories SET content = ?, tags = ?, importance = ?, updated_at = ?
               WHERE id = ?""",
            (new_content,
             json.dumps(new_tags) if new_tags else None,
             new_importance,
             time.time(),
             memory_id),
        )
        self._conn().commit()
        return self._parse_row(self._conn().execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone())

    def list_memories(
        self,
        platform: str,
        user_id: str,
        *,
        limit: int = 20,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        tag_clause = ""
        tag_params: list[str] = []
        if tags:
            placeholders = ",".join("?" for _ in tags)
            tag_clause = f"AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value IN ({placeholders}))"
            tag_params = list(tags)

        rows = self._conn().execute(
            f"""SELECT * FROM memories
                WHERE platform = ? AND user_id = ?
                {tag_clause}
                ORDER BY updated_at DESC
                LIMIT ?""",
            [platform, user_id, *tag_params, limit],
        ).fetchall()
        return [self._parse_row(r) for r in rows]

    def stats(self, platform: str, user_id: str) -> dict:
        row = self._conn().execute(
            """SELECT COUNT(*) as total,
                      MIN(created_at) as oldest,
                      MAX(created_at) as newest
               FROM memories WHERE platform = ? AND user_id = ?""",
            (platform, user_id),
        ).fetchone()
        return {"total": row["total"], "oldest": row["oldest"], "newest": row["newest"]}

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _to_fts_query(raw: str) -> str:
        tokens = raw.strip().split()
        if not tokens:
            return '""'
        return " ".join(f'"{t.replace(chr(34), chr(34)+chr(34))}"' for t in tokens)

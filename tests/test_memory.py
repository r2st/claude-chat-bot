"""
Comprehensive tests for MemoryStore — edge cases, concurrency, FTS,
special characters, and data integrity.

Run:
    pytest tests/test_memory.py -v
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest.mock

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "test_memory.db"))


# ══════════════════════════════════════════════════════════════════════════════
# 1. Basic CRUD
# ══════════════════════════════════════════════════════════════════════════════


class TestBasicCRUD:
    def test_remember_returns_memory(self, store):
        mem = store.remember("tg", "u1", "likes python")
        assert mem.id
        assert mem.platform == "tg"
        assert mem.user_id == "u1"
        assert mem.content == "likes python"
        assert mem.importance == 0.5
        assert mem.created_at > 0
        assert mem.updated_at > 0

    def test_remember_strips_whitespace(self, store):
        mem = store.remember("tg", "u1", "  padded content  \n")
        assert mem.content == "padded content"

    def test_recall_fts(self, store):
        store.remember("tg", "u1", "user prefers dark mode")
        results = store.recall("tg", "u1", "dark mode")
        assert len(results) >= 1
        assert any("dark" in r.content for r in results)

    def test_recall_empty_query_returns_all(self, store):
        store.remember("tg", "u1", "first")
        store.remember("tg", "u1", "second")
        results = store.recall("tg", "u1", "")
        assert len(results) >= 2

    def test_recall_no_results(self, store):
        results = store.recall("tg", "u1", "nonexistent_xyz_123")
        assert results == []

    def test_forget_returns_true(self, store):
        mem = store.remember("tg", "u1", "to forget")
        assert store.forget("tg", "u1", mem.id) is True

    def test_forget_nonexistent_returns_false(self, store):
        assert store.forget("tg", "u1", "no-such-id") is False

    def test_forget_wrong_platform(self, store):
        mem = store.remember("tg", "u1", "platform test")
        assert store.forget("slack", "u1", mem.id) is False

    def test_forget_wrong_user(self, store):
        mem = store.remember("tg", "u1", "user test")
        assert store.forget("tg", "u2", mem.id) is False

    def test_update_content(self, store):
        mem = store.remember("tg", "u1", "original")
        updated = store.update("tg", "u1", mem.id, content="changed")
        assert updated.content == "changed"
        assert updated.updated_at >= mem.updated_at

    def test_update_tags(self, store):
        mem = store.remember("tg", "u1", "tagged", tags=["a"])
        updated = store.update("tg", "u1", mem.id, tags=["b", "c"])
        assert updated.tags == ["b", "c"]

    def test_update_importance(self, store):
        mem = store.remember("tg", "u1", "imp test")
        updated = store.update("tg", "u1", mem.id, importance=0.9)
        assert updated.importance == 0.9

    def test_update_nonexistent(self, store):
        assert store.update("tg", "u1", "no-id", content="x") is None

    def test_list_memories(self, store):
        store.remember("tg", "u1", "mem a")
        store.remember("tg", "u1", "mem b")
        mems = store.list_memories("tg", "u1")
        assert len(mems) == 2

    def test_list_respects_limit(self, store):
        for i in range(10):
            store.remember("tg", "u1", f"mem {i}")
        mems = store.list_memories("tg", "u1", limit=3)
        assert len(mems) == 3

    def test_stats(self, store):
        store.remember("tg", "u1", "stat a")
        store.remember("tg", "u1", "stat b")
        s = store.stats("tg", "u1")
        assert s["total"] == 2
        assert s["oldest"] is not None
        assert s["newest"] is not None
        assert s["newest"] >= s["oldest"]

    def test_stats_empty_user(self, store):
        s = store.stats("tg", "nobody")
        assert s["total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# 2. Importance clamping
# ══════════════════════════════════════════════════════════════════════════════


class TestImportance:
    def test_clamp_high(self, store):
        mem = store.remember("tg", "u1", "high", importance=5.0)
        assert mem.importance == 1.0

    def test_clamp_low(self, store):
        mem = store.remember("tg", "u1", "low", importance=-3.0)
        assert mem.importance == 0.0

    def test_clamp_zero(self, store):
        mem = store.remember("tg", "u1", "zero", importance=0.0)
        assert mem.importance == 0.0

    def test_clamp_one(self, store):
        mem = store.remember("tg", "u1", "one", importance=1.0)
        assert mem.importance == 1.0

    def test_update_clamp_high(self, store):
        mem = store.remember("tg", "u1", "x")
        updated = store.update("tg", "u1", mem.id, importance=99.0)
        assert updated.importance == 1.0

    def test_update_clamp_low(self, store):
        mem = store.remember("tg", "u1", "x")
        updated = store.update("tg", "u1", mem.id, importance=-99.0)
        assert updated.importance == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. Tag filtering
# ══════════════════════════════════════════════════════════════════════════════


class TestTags:
    def test_recall_with_matching_tag(self, store):
        store.remember("tg", "u1", "tag match", tags=["pref"])
        store.remember("tg", "u1", "no tag match")
        results = store.recall("tg", "u1", "match", tags=["pref"])
        assert all("pref" in r.tags for r in results)

    def test_recall_with_nonmatching_tag(self, store):
        store.remember("tg", "u1", "only tagged", tags=["x"])
        results = store.recall("tg", "u1", "tagged", tags=["y"])
        assert len(results) == 0

    def test_list_with_tag_filter(self, store):
        store.remember("tg", "u1", "a", tags=["work"])
        store.remember("tg", "u1", "b", tags=["personal"])
        mems = store.list_memories("tg", "u1", tags=["work"])
        assert len(mems) == 1
        assert mems[0].tags == ["work"]

    def test_empty_tags_stored_as_list(self, store):
        mem = store.remember("tg", "u1", "no tags")
        assert mem.tags == []

    def test_multiple_tags(self, store):
        mem = store.remember("tg", "u1", "multi", tags=["a", "b", "c"])
        assert mem.tags == ["a", "b", "c"]


# ══════════════════════════════════════════════════════════════════════════════
# 4. Platform / user isolation
# ══════════════════════════════════════════════════════════════════════════════


class TestIsolation:
    def test_different_platforms_isolated(self, store):
        store.remember("telegram", "u1", "tg memory")
        store.remember("slack", "u1", "slack memory")
        tg = store.list_memories("telegram", "u1")
        sl = store.list_memories("slack", "u1")
        assert len(tg) == 1
        assert len(sl) == 1
        assert tg[0].content == "tg memory"
        assert sl[0].content == "slack memory"

    def test_different_users_isolated(self, store):
        store.remember("tg", "alice", "alice mem")
        store.remember("tg", "bob", "bob mem")
        assert len(store.list_memories("tg", "alice")) == 1
        assert len(store.list_memories("tg", "bob")) == 1

    def test_recall_only_own_platform(self, store):
        store.remember("tg", "u1", "telegram specific content")
        results = store.recall("slack", "u1", "telegram specific")
        assert len(results) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 5. Special characters and edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestSpecialContent:
    def test_unicode_content(self, store):
        mem = store.remember("tg", "u1", "loves sushi")
        assert mem.content == "loves sushi"

    def test_quotes_in_content(self, store):
        mem = store.remember("tg", "u1", 'said "hello world"')
        assert '"hello' in mem.content

    def test_single_quotes(self, store):
        mem = store.remember("tg", "u1", "it's fine")
        assert mem.content == "it's fine"

    def test_newlines_in_content(self, store):
        mem = store.remember("tg", "u1", "line1\nline2\nline3")
        assert "\n" in mem.content

    def test_sql_injection_attempt(self, store):
        mem = store.remember("tg", "u1", "'; DROP TABLE memories; --")
        assert mem.content == "'; DROP TABLE memories; --"
        mems = store.list_memories("tg", "u1")
        assert len(mems) >= 1

    def test_html_in_content(self, store):
        mem = store.remember("tg", "u1", "<script>alert('xss')</script>")
        assert "<script>" in mem.content

    def test_very_long_content(self, store):
        long_text = "x" * 10000
        mem = store.remember("tg", "u1", long_text)
        assert len(mem.content) == 10000

    def test_empty_string_content(self, store):
        mem = store.remember("tg", "u1", "")
        assert mem.content == ""

    def test_whitespace_only_content(self, store):
        mem = store.remember("tg", "u1", "   ")
        assert mem.content == ""

    def test_special_fts_chars_in_query(self, store):
        store.remember("tg", "u1", "regular content here")
        results = store.recall("tg", "u1", "content AND here OR (not)")
        # Should not crash — FTS query gets quoted

    def test_tags_with_special_chars(self, store):
        mem = store.remember("tg", "u1", "tag test", tags=["a:b", "c/d", "e f"])
        assert mem.tags == ["a:b", "c/d", "e f"]


# ══════════════════════════════════════════════════════════════════════════════
# 6. FTS search quality
# ══════════════════════════════════════════════════════════════════════════════


class TestFTSSearch:
    def test_stemming_matches(self, store):
        store.remember("tg", "u1", "user is running fast")
        results = store.recall("tg", "u1", "run")
        assert len(results) >= 1

    def test_partial_word_match(self, store):
        store.remember("tg", "u1", "programming in python")
        results = store.recall("tg", "u1", "python")
        assert len(results) >= 1

    def test_multiple_word_query(self, store):
        store.remember("tg", "u1", "prefers dark mode with blue accents")
        results = store.recall("tg", "u1", "dark blue")
        assert len(results) >= 1

    def test_recall_respects_limit(self, store):
        for i in range(20):
            store.remember("tg", "u1", f"item number {i}")
        results = store.recall("tg", "u1", "item", limit=5)
        assert len(results) <= 5

    def test_fts_query_quoting(self):
        assert MemoryStore._to_fts_query("hello world") == '"hello" "world"'
        assert MemoryStore._to_fts_query("  spaces  ") == '"spaces"'
        assert MemoryStore._to_fts_query("") == '""'

    def test_fts_query_double_quotes(self):
        result = MemoryStore._to_fts_query('say "hi"')
        assert '""' in result  # quotes get doubled


# ══════════════════════════════════════════════════════════════════════════════
# 7. Concurrency
# ══════════════════════════════════════════════════════════════════════════════


class TestConcurrency:
    def test_concurrent_writes(self, store):
        errors = []

        def writer(platform, uid, n):
            try:
                for i in range(20):
                    store.remember(platform, uid, f"concurrent write {n}-{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("tg", "u1", i))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        mems = store.list_memories("tg", "u1", limit=200)
        assert len(mems) == 100  # 5 threads * 20 writes

    def test_concurrent_read_write(self, store):
        store.remember("tg", "u1", "initial memory for reads")
        errors = []

        def reader():
            try:
                for _ in range(20):
                    store.recall("tg", "u1", "initial")
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(20):
                    store.remember("tg", "u1", f"extra {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads += [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors

    def test_concurrent_forget(self, store):
        mems = [store.remember("tg", "u1", f"to delete {i}") for i in range(10)]
        errors = []

        def forgetter(mem):
            try:
                store.forget("tg", "u1", mem.id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=forgetter, args=(m,)) for m in mems]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert store.stats("tg", "u1")["total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# 8. Data integrity
# ══════════════════════════════════════════════════════════════════════════════


class TestDataIntegrity:
    def test_unique_ids(self, store):
        ids = set()
        for i in range(50):
            mem = store.remember("tg", "u1", f"unique {i}")
            assert mem.id not in ids
            ids.add(mem.id)

    def test_timestamps_increase(self, store):
        m1 = store.remember("tg", "u1", "first")
        time.sleep(0.01)
        m2 = store.remember("tg", "u1", "second")
        assert m2.created_at >= m1.created_at

    def test_update_preserves_unmodified_fields(self, store):
        mem = store.remember("tg", "u1", "original", tags=["keep"], importance=0.8)
        updated = store.update("tg", "u1", mem.id, content="new content")
        assert updated.tags == ["keep"]
        assert updated.importance == 0.8

    def test_forgotten_memory_not_in_recall(self, store):
        mem = store.remember("tg", "u1", "ephemeral thing")
        store.forget("tg", "u1", mem.id)
        results = store.recall("tg", "u1", "ephemeral")
        assert not any(r.id == mem.id for r in results)

    def test_forgotten_memory_not_in_list(self, store):
        mem = store.remember("tg", "u1", "gone")
        store.forget("tg", "u1", mem.id)
        mems = store.list_memories("tg", "u1")
        assert not any(m.id == mem.id for m in mems)

    def test_list_ordered_by_updated_at(self, store):
        store.remember("tg", "u1", "old")
        time.sleep(0.01)
        store.remember("tg", "u1", "new")
        mems = store.list_memories("tg", "u1")
        assert mems[0].content == "new"
        assert mems[1].content == "old"


# ══════════════════════════════════════════════════════════════════════════════
# 9. LIKE fallback when FTS unavailable
# ══════════════════════════════════════════════════════════════════════════════


class TestLikeFallback:
    def test_recall_without_fts_uses_like(self, store):
        store.remember("tg", "u1", "python is great")
        with unittest.mock.patch.object(store, "_has_fts", return_value=False):
            results = store.recall("tg", "u1", "python")
        assert len(results) >= 1
        assert any("python" in r.content for r in results)

    def test_recall_without_fts_no_match(self, store):
        store.remember("tg", "u1", "java is okay")
        with unittest.mock.patch.object(store, "_has_fts", return_value=False):
            results = store.recall("tg", "u1", "python")
        assert len(results) == 0

    def test_recall_without_fts_with_tags(self, store):
        store.remember("tg", "u1", "tagged content", tags=["lang"])
        store.remember("tg", "u1", "untagged content")
        with unittest.mock.patch.object(store, "_has_fts", return_value=False):
            results = store.recall("tg", "u1", "content", tags=["lang"])
        assert len(results) == 1
        assert results[0].content == "tagged content"

    def test_has_fts_returns_false_when_fts_broken(self, store):
        mock_conn = unittest.mock.MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such table")
        with unittest.mock.patch.object(store, "_conn", return_value=mock_conn):
            assert store._has_fts() is False

    def test_recall_without_fts_respects_limit(self, store):
        for i in range(10):
            store.remember("tg", "u1", f"item number {i}")
        with unittest.mock.patch.object(store, "_has_fts", return_value=False):
            results = store.recall("tg", "u1", "item", limit=3)
        assert len(results) <= 3

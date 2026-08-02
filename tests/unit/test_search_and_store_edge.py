"""Tests for session.search integration with real SQLiteStore + FTS5.

Covers search_sessions end-to-end (FTS5 ranking + LIKE fallback), the
_store serialization helpers, and SQLiteStore edge cases not covered
elsewhere.
"""

import json
import pytest

from microagent.core.store import SQLiteStore, _serialize_message, _deserialize_message
from microagent.core.types import Message, ToolCall, ToolResult, Usage
from microagent.session.search import _build_fts_query, ensure_fts5, search_sessions


class TestMessageSerialization:
    def test_roundtrip_simple(self):
        m = Message.user("hello world")
        data = _serialize_message(m)
        assert isinstance(data, str)
        restored = _deserialize_message(data)
        assert restored.role == "user"
        assert restored.content == "hello world"

    def test_roundtrip_tool_call(self):
        m = Message.assistant(
            text="thinking",
            tool_calls=(ToolCall(id="c1", name="bash", arguments={"command": "ls"}),),
        )
        restored = _deserialize_message(_serialize_message(m))
        assert restored.tool_calls == m.tool_calls

    def test_roundtrip_tool_result(self):
        m = Message.tool_result(ToolResult.ok("out"), tool_call_id="c1")
        restored = _deserialize_message(_serialize_message(m))
        assert restored.role == "tool"
        assert restored.tool_call_id == "c1"

    def test_roundtrip_usage(self):
        m = Message.assistant(text="x", usage=Usage(input_tokens=10, output_tokens=5))
        restored = _deserialize_message(_serialize_message(m))
        assert restored.usage == m.usage

    def test_invalid_json_raises(self):
        with pytest.raises(Exception):
            _deserialize_message("not json {{{")


class TestStoreEdgeCases:
    async def test_checkpoint(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("a"))
        await store.checkpoint("s1")
        store.close()

    async def test_session_summaries(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("hello world"))
        await store.append("s1", Message.assistant("hi there"))
        await store.append("s2", Message.user("second session"))
        summaries = await store.session_summaries()
        assert len(summaries) == 2
        by_id = {s["session_id"]: s for s in summaries}
        assert by_id["s1"]["count"] == 2
        # preview is the most recent message's content
        assert "hi there" in by_id["s1"]["preview"]
        store.close()

    async def test_append_after_close_raises(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        store.close()
        with pytest.raises(Exception):
            await store.append("s1", Message.user("x"))

    async def test_serialize_binary_content(self):
        m = Message.user("你好世界 \x00 binary-ish")
        restored = _deserialize_message(_serialize_message(m))
        assert restored.content == m.content


class TestSearchSessions:
    async def test_fts5_search_finds_messages(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("the quick brown fox jumps"))
        await store.append("s1", Message.assistant("nothing relevant here"))
        await store.append("s2", Message.user("docker containers orchestration"))

        results = await search_sessions(store, "docker", k=5)
        assert len(results) >= 1
        assert "docker" in results[0].content.lower()

    async def test_fts5_search_no_results(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("alpha beta"))
        results = await search_sessions(store, "zzzz-no-match-xyz", k=5)
        assert results == ()

    async def test_like_fallback_on_fts_error(self, tmp_path):
        """Force FTS5 to fail → LIKE fallback still returns matches."""
        from microagent.session import search as search_mod
        real_store = SQLiteStore(tmp_path / "s.db")
        await real_store.append("s1", Message.user("needle in a haystack"))

        class _BrokenConn:
            """Wraps a real connection but fails on MATCH queries."""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if "MATCH" in sql:
                    raise Exception("fts5 unavailable")
                return self._real.execute(sql, *args, **kwargs)

        # Patch ensure_fts5 to no-op and inject the broken connection
        orig_ensure = search_mod.ensure_fts5
        orig_conn = real_store._conn
        search_mod.ensure_fts5 = lambda store: None
        real_store._conn = _BrokenConn(orig_conn)
        try:
            results = await search_sessions(real_store, "needle", k=5)
            assert len(results) >= 1
            assert "needle" in results[0].content
        finally:
            real_store._conn = orig_conn
            search_mod.ensure_fts5 = orig_ensure
            real_store.close()

    async def test_non_sqlite_store_returns_empty(self):
        from microagent.core.store import InMemoryStore
        store = InMemoryStore()
        results = await search_sessions(store, "anything", k=5)
        assert results == ()

    def test_ensure_fts5_idempotent(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        ensure_fts5(store)  # first call
        ensure_fts5(store)  # second call must not error
        store.close()

    def test_build_fts_query_strips_special(self):
        q = _build_fts_query('docker "quoted" (paren) *star')
        # FTS5 special chars are stripped from the output
        assert "(" not in q and ")" not in q and "*" not in q
        assert "docker" in q

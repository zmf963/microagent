"""Tests for SQLiteStore and InMemoryStore."""

from microagent.core.store import InMemoryStore, SQLiteStore
from microagent.core.types import Message, ToolCall, ToolResult, Usage


class TestInMemoryStore:
    async def test_append_load(self):
        store = InMemoryStore()
        await store.append("s1", Message.user("hello"))
        await store.append("s1", Message.assistant("hi"))
        history = await store.load_history("s1")
        assert len(history) == 2
        assert history[0].content == "hello"
        assert history[1].content == "hi"

    async def test_empty_session(self):
        store = InMemoryStore()
        history = await store.load_history("nonexistent")
        assert history == []

    async def test_multiple_sessions(self):
        store = InMemoryStore()
        await store.append("s1", Message.user("msg1"))
        await store.append("s2", Message.user("msg2"))
        assert len(await store.load_history("s1")) == 1
        assert len(await store.load_history("s2")) == 1

    async def test_list_sessions(self):
        store = InMemoryStore()
        await store.append("s1", Message.user("a"))
        await store.append("s2", Message.user("b"))
        sessions = await store.list_sessions()
        assert set(sessions) == {"s1", "s2"}


class TestSQLiteStore:
    async def test_roundtrip(self, tmp_path):
        store = SQLiteStore(tmp_path / "test.db")
        await store.append("s1", Message.user("hello"))
        await store.append("s1", Message.assistant("world"))
        history = await store.load_history("s1")
        assert len(history) == 2
        assert history[0].content == "hello"
        store.close()

    async def test_load_history_skips_corrupt_rows(self, tmp_path):
        """A corrupt row must not kill the whole session history.

        Mirrors the per-row try/except already used by session_summaries:
        one bad JSON blob (disk corruption, partial write) should be
        skipped, not crash CLI resume / cron / SessionRunner.load_history.
        """
        import sqlite3

        from microagent.core.store import _serialize_message

        db_path = tmp_path / "corrupt.db"
        store = SQLiteStore(db_path)
        await store.append("s1", Message.user("good1"))
        await store.append("s1", Message.user("bad-row"))
        await store.append("s1", Message.user("good2"))
        store.close()

        # Corrupt the second row in place (simulates disk corruption /
        # interrupted write on an existing row)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE messages SET data = '{not-valid-json{{{' WHERE session_id = 's1' AND seq = 2"
        )
        conn.commit()
        conn.close()

        store2 = SQLiteStore(db_path)
        history = await store2.load_history("s1")
        contents = [m.content for m in history]
        assert "good1" in contents and "good2" in contents
        assert len(history) == 2
        store2.close()

    async def test_load_history_all_corrupt_returns_empty(self, tmp_path):
        """If every row is corrupt, load_history returns [] not an exception."""
        import sqlite3

        db_path = tmp_path / "allbad.db"
        store = SQLiteStore(db_path)
        await store.append("s1", Message.user("seed"))  # creates the table
        store.close()

        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE messages SET data = 'garbage'")
        conn.commit()
        conn.close()

        store2 = SQLiteStore(db_path)
        history = await store2.load_history("s1")
        assert history == []
        store2.close()

    async def test_persistence_across_connections(self, tmp_path):
        db_path = tmp_path / "persist.db"
        store1 = SQLiteStore(db_path)
        await store1.append("s1", Message.user("persistent"))
        await store1.checkpoint("s1")
        store1.close()

        store2 = SQLiteStore(db_path)
        history = await store2.load_history("s1")
        assert len(history) == 1
        assert history[0].content == "persistent"
        store2.close()

    async def test_tool_calls_roundtrip(self, tmp_path):
        store = SQLiteStore(tmp_path / "tools.db")
        tc = ToolCall(id="call_1", name="bash", arguments={"command": "ls"})
        msg = Message.assistant("thinking", tool_calls=(tc,))
        await store.append("s1", msg)

        result = ToolResult.ok("output")
        await store.append("s1", Message.tool_result(result, tool_call_id="call_1"))

        history = await store.load_history("s1")
        assert len(history) == 2
        assert history[0].tool_calls[0].name == "bash"
        assert history[1].tool_call_id == "call_1"
        store.close()

    async def test_usage_roundtrip(self, tmp_path):
        store = SQLiteStore(tmp_path / "usage.db")
        usage = Usage(input_tokens=100, output_tokens=50, cost_usd=0.01)
        await store.append("s1", Message.assistant("resp", usage=usage))
        history = await store.load_history("s1")
        assert history[0].usage is not None
        assert history[0].usage.input_tokens == 100
        store.close()

    async def test_is_error_roundtrip(self, tmp_path):
        """is_error field must survive serialization/deserialization."""
        store = SQLiteStore(tmp_path / "error.db")
        err_result = ToolResult.error("something went wrong")
        await store.append("s1", Message.tool_result(err_result, tool_call_id="c1"))
        history = await store.load_history("s1")
        assert history[0].is_error is True
        store.close()

    async def test_list_sessions(self, tmp_path):
        store = SQLiteStore(tmp_path / "multi.db")
        await store.append("s1", Message.user("a"))
        await store.append("s2", Message.user("b"))
        sessions = await store.list_sessions()
        assert set(sessions) == {"s1", "s2"}
        store.close()

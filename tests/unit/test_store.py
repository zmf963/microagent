"""Tests for SQLiteStore and InMemoryStore."""

import pytest
from microagent.core.store import SQLiteStore, InMemoryStore
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

    async def test_list_sessions(self, tmp_path):
        store = SQLiteStore(tmp_path / "multi.db")
        await store.append("s1", Message.user("a"))
        await store.append("s2", Message.user("b"))
        sessions = await store.list_sessions()
        assert set(sessions) == {"s1", "s2"}
        store.close()

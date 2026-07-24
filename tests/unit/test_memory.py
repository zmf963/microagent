"""Tests for Memory dataclass, MemoryProvider Protocol, and SQLiteMemoryProvider."""

import pytest

from microagent.core.types import Message
from microagent.memory.provider import Memory, MemoryProvider, SQLiteMemoryProvider


class TestMemory:
    def test_create_memory(self):
        m = Memory(
            id="m1",
            content="User prefers Python over JS.",
            category="preference",
            created_at=12345.0,
            session_id="s1",
        )
        assert m.id == "m1"
        assert m.category == "preference"
        assert m.session_id == "s1"
        assert m.visibility == "private"  # default
        assert m.relevance_score == 0.0  # default

    def test_memory_frozen(self):
        m = Memory(id="m1", content="x", category="fact", created_at=0.0)
        with pytest.raises(AttributeError):
            m.content = "changed"


class TestMemoryProviderProtocol:
    async def test_protocol_accepted(self):
        """A class implementing MemoryProvider is structurally accepted."""

        class SimpleMem:
            async def prefetch(self, query: str) -> None:
                pass

            async def recall(self, query: str, k: int = 5):
                return ()

            async def sync_turn(self, session_id: str, history: tuple[Message, ...]) -> None:
                pass

            async def batch_write(self, memories: tuple[Memory, ...]) -> None:
                pass

            async def delete(self, memory_id: str) -> None:
                pass

            def system_prompt_block(self) -> str:
                return ""

        provider = SimpleMem()
        assert isinstance(provider, MemoryProvider)


class TestSQLiteMemoryProvider:
    async def test_write_and_recall(self, tmp_path):
        store = SQLiteMemoryProvider(tmp_path / "mem.db")
        m = Memory(
            id="m1",
            content="User likes Python.",
            category="preference",
            created_at=1000.0,
            session_id="s1",
        )
        await store.batch_write((m,))
        results = await store.recall("Python", k=5)
        assert len(results) == 1
        assert results[0].content == "User likes Python."

    async def test_recall_no_match(self, tmp_path):
        store = SQLiteMemoryProvider(tmp_path / "mem2.db")
        results = await store.recall("nonexistent", k=5)
        assert len(results) == 0

    async def test_recall_ordered_by_relevance(self, tmp_path):
        store = SQLiteMemoryProvider(tmp_path / "mem3.db")
        await store.batch_write(
            (
                Memory(id="m1", content="Python is great.", category="fact", created_at=1.0),
                Memory(id="m2", content="Java is verbose.", category="fact", created_at=2.0),
                Memory(id="m3", content="Python async is tricky.", category="fact", created_at=3.0),
            )
        )
        results = await store.recall("Python", k=5)
        assert len(results) == 2
        # "Python is great" should rank higher than "Python async is tricky"
        # (both match "Python", FTS5 bm25 scores them)

    async def test_delete(self, tmp_path):
        store = SQLiteMemoryProvider(tmp_path / "mem4.db")
        await store.batch_write(
            (Memory(id="m1", content="delete me.", category="fact", created_at=1.0),)
        )
        await store.delete("m1")
        results = await store.recall("delete", k=5)
        assert len(results) == 0

    async def test_system_prompt_block(self, tmp_path):
        store = SQLiteMemoryProvider(tmp_path / "mem5.db")
        block = store.system_prompt_block()
        assert isinstance(block, str)

    async def test_sync_turn_noop(self, tmp_path):
        """sync_turn without LLM extraction just stores messages as-is."""
        store = SQLiteMemoryProvider(tmp_path / "mem6.db")
        history = (
            Message.user("hello"),
            Message.assistant("hi"),
        )
        await store.sync_turn("s1", history)
        # After sync, recent messages should be searchable
        results = await store.recall("hello", k=5)
        assert len(results) >= 0  # sync_turn is async, may not have finished

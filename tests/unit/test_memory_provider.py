"""Tests for SQLiteMemoryProvider — FTS5 search, write, recall."""


import pytest

from microagent.memory.provider import Memory, SQLiteMemoryProvider


@pytest.fixture
def provider(tmp_path):
    path = tmp_path / "test_memory.db"
    p = SQLiteMemoryProvider(path)
    yield p
    p.close()


class TestMemoryData:
    def test_create_memory(self):
        m = Memory(id="m1", content="test fact", category="fact", created_at=1.0)
        assert m.id == "m1"
        assert m.category == "fact"

    def test_memory_defaults(self):
        m = Memory(id="x", content="hello", category="preference", created_at=0.0)
        assert m.relevance_score == 0.0
        assert m.session_id is None
        assert m.visibility == "private"


class TestProviderWrite:
    async def test_batch_write_and_recall(self, provider):
        mems = (
            Memory(id="a", content="Python 3.14", category="fact", created_at=1.0),
            Memory(id="b", content="Docker compose", category="fact", created_at=2.0),
        )
        await provider.batch_write(mems)
        results = await provider.recall("Python", k=5)
        assert len(results) == 1
        assert results[0].content == "Python 3.14"

    async def test_recall_multiple(self, provider):
        await provider.batch_write(
            (
                Memory(id="a", content="Kubernetes pods", category="fact", created_at=1.0),
                Memory(id="b", content="Docker pods", category="fact", created_at=2.0),
            )
        )
        results = await provider.recall("pods", k=5)
        assert len(results) == 2

    async def test_recall_no_match(self, provider):
        await provider.batch_write(
            (Memory(id="x", content="some data", category="fact", created_at=1.0),)
        )
        results = await provider.recall("zzz", k=5)
        assert len(results) == 0

    async def test_recall_limit(self, provider):
        mems = tuple(
            Memory(id=f"m{i}", content=f"item {i}", category="fact", created_at=float(i))
            for i in range(10)
        )
        await provider.batch_write(mems)
        results = await provider.recall("item", k=3)
        assert len(results) == 3

    async def test_delete(self, provider):
        await provider.batch_write(
            (Memory(id="delme", content="remove this", category="fact", created_at=1.0),)
        )
        await provider.delete("delme")
        results = await provider.recall("remove", k=1)
        assert len(results) == 0

    async def test_sync_turn(self, provider):
        from microagent.core.types import Message

        msgs = (
            Message.user("build the docker image"),
            Message.assistant("ok running docker build"),
        )
        await provider.sync_turn("sess-1", msgs)
        results = await provider.recall("docker", k=5)
        assert len(results) >= 2

    async def test_system_prompt_block_default_empty(self, provider):
        assert provider.system_prompt_block() == ""

    async def test_prefetch_noop(self, provider):
        # prefetch is a noop — just ensure it doesn't crash
        await provider.prefetch("anything")

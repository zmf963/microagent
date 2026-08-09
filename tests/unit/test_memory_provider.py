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

    async def test_recall_empty_query_returns_nothing(self, provider):
        """FTS5 MATCH '' matches every row — an empty/blank query would
        leak ALL memories into context. Must return () instead."""
        await provider.batch_write(
            (
                Memory(id="a", content="secret one", category="fact", created_at=1.0),
                Memory(id="b", content="secret two", category="fact", created_at=2.0),
            )
        )
        assert await provider.recall("", k=5) == ()
        assert await provider.recall("   ", k=5) == ()
        assert await provider.recall("\n\t", k=5) == ()
        # Non-empty queries still work
        results = await provider.recall("secret", k=5)
        assert len(results) == 2

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


class TestProviderInsertSemantics:
    async def test_rewrite_same_id_clears_stale_fts(self, provider):
        """INSERT OR REPLACE used to change the rowid and orphan the old
        FTS entry — a rewrite must clear the stale text from the index."""
        await provider.batch_write((
            Memory(id="m1", content="alpha unicorn fact", category="fact", created_at=1.0),
        ))
        await provider.batch_write((
            Memory(id="m1", content="beta dragon fact", category="fact", created_at=2.0),
        ))
        assert len(await provider.recall("beta", k=5)) == 1
        # Stale content must be gone from the FTS index
        assert len(await provider.recall("unicorn", k=5)) == 0

    async def test_idempotent_rewrite_same_content(self, provider):
        """Re-writing identical (id, content) is a no-op — the FTS index
        must not accumulate duplicate entries."""
        mem = Memory(id="m1", content="same content here", category="fact", created_at=1.0)
        await provider.batch_write((mem,))
        await provider.batch_write((mem,))
        results = await provider.recall("same content", k=5)
        assert len(results) == 1

    async def test_recall_respects_lock(self, provider):
        """recall runs sync sqlite3 under the provider lock — holding the
        lock must block it, proving it no longer runs on the event loop."""
        import asyncio

        await provider.batch_write((
            Memory(id="m1", content="lock test fact", category="fact", created_at=1.0),
        ))
        await provider._lock.acquire()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(provider.recall("lock", k=1), timeout=0.3)
        finally:
            provider._lock.release()
        results = await provider.recall("lock", k=1)
        assert len(results) == 1


class TestContentHashDedupe:
    async def test_exact_duplicate_skipped(self, provider):
        """Same content with different ids (extractor uses fresh uuids each
        turn) must dedupe to one row."""
        await provider.batch_write((
            Memory(id="a", content="user prefers dark mode", category="preference", created_at=1.0),
        ))
        await provider.batch_write((
            Memory(id="b", content="user prefers dark mode", category="preference", created_at=2.0),
        ))
        results = await provider.recall("dark mode", k=5)
        assert len(results) == 1

    async def test_case_and_whitespace_variants_deduped(self, provider):
        """Normalization: case + whitespace differences don't defeat dedupe."""
        await provider.batch_write((
            Memory(id="a", content="User Prefers Dark Mode", category="preference", created_at=1.0),
        ))
        await provider.batch_write((
            Memory(id="b", content="  user   prefers  dark mode \n", category="preference", created_at=2.0),
        ))
        results = await provider.recall("dark mode", k=5)
        assert len(results) == 1

    async def test_genuine_revisions_kept(self, provider):
        """Punctuation/word-order differences are NOT duplicates — the
        normalization is deliberately conservative."""
        await provider.batch_write((
            Memory(id="a", content="deploy to production, then staging", category="task", created_at=1.0),
        ))
        await provider.batch_write((
            Memory(id="b", content="deploy to staging then production", category="task", created_at=2.0),
        ))
        results = await provider.recall("deploy", k=5)
        assert len(results) == 2

    async def test_migration_backfills_existing_rows(self, tmp_path):
        """An old DB (no content_hash column) gains it on open, with
        existing rows backfilled."""
        import sqlite3

        path = tmp_path / "old.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, category TEXT NOT NULL,
            created_at REAL NOT NULL, session_id TEXT,
            visibility TEXT NOT NULL DEFAULT 'private', metadata TEXT
        );
        CREATE VIRTUAL TABLE memories_fts USING fts5(
            content, content='memories', content_rowid='rowid'
        );
        """)
        conn.execute(
            "INSERT INTO memories (id, content, category, created_at) "
            "VALUES ('old1', 'legacy fact here', 'fact', 1.0)"
        )
        conn.commit()
        conn.close()

        prov = SQLiteMemoryProvider(path)
        cols = {r[1] for r in prov._conn.execute("PRAGMA table_info(memories)")}
        assert "content_hash" in cols
        row = prov._conn.execute(
            "SELECT content_hash FROM memories WHERE id = 'old1'"
        ).fetchone()
        assert row[0] == SQLiteMemoryProvider._content_hash("legacy fact here")
        # And dedupe works against the backfilled row
        await prov.batch_write((
            Memory(id="new1", content="Legacy Fact Here", category="fact", created_at=2.0),
        ))
        assert len(prov._conn.execute("SELECT id FROM memories").fetchall()) == 1
        prov.close()

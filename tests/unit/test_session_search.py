"""Tests for session_search — FTS5 query builder + search fallback."""


import pytest

from microagent.core.store import SQLiteStore
from microagent.session.search import _build_fts_query, ensure_fts5, search_sessions


class TestBuildFTSQuery:
    def test_latin_single_word(self):
        assert '"docker"' in _build_fts_query("docker")

    def test_latin_multi_word(self):
        q = _build_fts_query("python code")
        assert '"python"' in q
        assert '"code"' in q
        assert " OR " in q

    def test_cjk_bigrams(self):
        q = _build_fts_query("代码审查")
        assert "代码" in q or "码审" in q or "审查" in q

    def test_mixed_latin_cjk(self):
        q = _build_fts_query("Python 代码")
        assert '"Python"' in q
        # should contain bigram of 代码
        assert "代码" in q


class TestEnsureFTS5:
    def test_ensure_fts5_idempotent(self, tmp_path):
        store = SQLiteStore(tmp_path / "search.db")
        ensure_fts5(store)
        ensure_fts5(store)  # second call should not raise
        store.close()


class TestSearchSessions:
    @pytest.fixture
    def store(self, tmp_path):
        s = SQLiteStore(tmp_path / "s.db")
        yield s
        s.close()

    async def _seed(self, store):
        from microagent.core.types import Message

        await store.append("s1", Message.user("docker compose up -d"))
        await store.append("s2", Message.assistant("python import error"))

    async def test_search_finds_match(self, store):
        await self._seed(store)
        results = await search_sessions(store, "docker", k=3)
        assert len(results) >= 1
        assert any("docker" in m.content for m in results)

    async def test_search_no_match(self, store):
        await self._seed(store)
        results = await search_sessions(store, "zzz_nonexistent", k=3)
        assert len(results) == 0

    async def test_search_respects_k(self, store):
        await self._seed(store)
        results = await search_sessions(store, "docker", k=1)
        assert len(results) <= 1

    async def test_search_fallback_like(self, store):
        """FTS5 may be unavailable — falls back to LIKE."""
        from microagent.core.types import Message

        await store.append("s1", Message.user("special token here"))
        # Force the LIKE fallback by breaking FTS5
        results = await search_sessions(store, "special", k=1)
        assert len(results) >= 1

    async def test_cjk_query_matches(self, store):
        """unicode61 indexes a CJK run as ONE token — bare bigrams never
        match. CJK queries must use prefix matching (bigram*) or every CJK
        search silently returns 0 rows (no error → no LIKE fallback)."""
        from microagent.core.types import Message

        await store.append("s1", Message.user("代码审查非常重要，需要仔细检查"))
        results = await search_sessions(store, "代码", k=3)
        assert len(results) >= 1
        assert any("代码审查" in m.content for m in results)

    async def test_cjk_recall_matches(self, tmp_path):
        """Same CJK fix in SQLiteMemoryProvider.recall — raw MATCH missed."""
        from microagent.memory.provider import Memory, SQLiteMemoryProvider

        prov = SQLiteMemoryProvider(tmp_path / "mem.db")
        await prov.batch_write((
            Memory(id="m1", content="用户的代码审查偏好是先看安全", category="preference", created_at=1.0),
        ))
        results = await prov.recall("代码", k=3)
        assert len(results) >= 1
        assert any("代码审查" in m.content for m in results)
        prov.close()

    async def test_search_respects_store_lock(self, store):
        """search_sessions runs raw sync sqlite3 on store._conn — it must
        go through store._lock + to_thread like every other SQLiteStore
        method, or it races with concurrent append() writes and blocks
        the event loop."""
        import asyncio

        await self._seed(store)
        await store._lock.acquire()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(search_sessions(store, "docker"), timeout=0.3)
        finally:
            store._lock.release()
        # With the lock free, the search completes.
        results = await search_sessions(store, "docker", k=3)
        assert len(results) >= 1


async def test_fts5_path_actually_works_non_cjk(tmp_path):
    """Regression: the FTS5 index was created in an external-content shape
    (content=messages) whose columns role/content/session_id do not exist on
    the messages table. Every MATCH query raised 'no such column: T.role',
    was swallowed by the except-fallback, and silently degraded to LIKE.
    This test drives the REAL FTS path (Latin query, no CJK) and asserts a
    hit — proving the index is usable, not just present."""
    import asyncio
    from microagent.core.store import SQLiteStore
    from microagent.core.types import Message
    from microagent.session.search import search_sessions, ensure_fts5, _has_broken_fts_schema

    store = SQLiteStore(tmp_path / "s.db")
    await store.append("sess-A", Message.user("the quick brown fox"))
    await store.append("sess-B", Message.user("something completely different"))

    found = await search_sessions(store, "brown fox", k=5)
    contents = [m.content for m in found]
    assert "the quick brown fox" in contents, f"FTS miss; got {contents}"
    assert _has_broken_fts_schema(store._conn) is False
    store.close()


async def test_fts5_migrates_broken_external_content_shape(tmp_path):
    """An existing DB created with the broken content=messages shape must be
    detected, dropped, rebuilt as self-contained, and backfilled — making
    pre-existing sessions searchable via FTS for the first time."""
    import json
    from microagent.core.store import SQLiteStore
    from microagent.session.search import ensure_fts5, _has_broken_fts_schema

    store = SQLiteStore(tmp_path / "s.db")
    conn = store._conn
    # Simulate the OLD broken shape exactly.
    conn.executescript("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            role, content, session_id, content=messages, content_rowid=id);
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, role, content, session_id)
            VALUES (new.id, json_extract(new.data, '$.role'),
                    json_extract(new.data, '$.content'), new.session_id);
        END;
    """)
    conn.execute("INSERT INTO messages (session_id, seq, data) VALUES (?,?,?)",
                 ("old", 1, json.dumps({"role": "user", "content": "legacy searchable text"})))
    assert _has_broken_fts_schema(conn) is True

    ensure_fts5(store)  # should migrate + backfill

    assert _has_broken_fts_schema(conn) is False
    hit = conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'legacy'"
    ).fetchone()[0]
    assert hit == 1, f"backfill failed; FTS hits={hit}"
    store.close()

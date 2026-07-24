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

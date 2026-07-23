"""Tests for session_search — FTS5 search across all past sessions."""

import pytest
from pathlib import Path
from microagent.core.store import SQLiteStore
from microagent.session.search import search_sessions
from microagent.core.types import Message


class TestSessionSearch:
    async def test_search_finds_message(self, tmp_path):
        """Search finds messages across sessions."""
        store = SQLiteStore(tmp_path / "search.db")
        await store.append("s1", Message.user("Python is a programming language."))
        await store.append("s2", Message.user("I prefer Rust for systems programming."))
        await store.checkpoint("s1")
        await store.checkpoint("s2")

        results = await search_sessions(store, "Python", k=5)
        assert len(results) >= 1
        assert any("Python" in r.content for r in results)

    async def test_search_no_match(self, tmp_path):
        store = SQLiteStore(tmp_path / "nomatch.db")
        await store.append("s1", Message.user("hello"))
        await store.checkpoint("s1")

        results = await search_sessions(store, "nonexistent_term_xyz", k=5)
        assert len(results) == 0

    async def test_search_respects_k(self, tmp_path):
        store = SQLiteStore(tmp_path / "limit.db")
        for i in range(10):
            await store.append("s1", Message.user(f"test message number {i}"))
        await store.checkpoint("s1")

        results = await search_sessions(store, "test", k=3)
        assert len(results) <= 3

    async def test_search_finds_assistant_messages(self, tmp_path):
        store = SQLiteStore(tmp_path / "assistant.db")
        await store.append("s1", Message.user("what is Python?"))
        await store.append("s1", Message.assistant("Python is a programming language."))
        await store.checkpoint("s1")

        results = await search_sessions(store, "programming language", k=5)
        assert len(results) >= 1

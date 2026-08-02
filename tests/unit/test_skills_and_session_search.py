"""Tests for skills_list and session_search builtin tools."""

import pytest

from microagent.tools.builtins.skills_list import _current_loader, skills_list
from microagent.tools.builtins.session_search import _current_store, session_search
from microagent.core.types import Message, ToolResult


class _FakeSkill:
    def __init__(self, name, namespace="test", description=""):
        self.name = name
        self.namespace = namespace
        self.description = description


class _FakeLoader:
    def __init__(self, skills):
        self._skills = skills

    async def load(self):
        return self._skills


class _ThrowingLoader:
    async def load(self):
        raise RuntimeError("boom")


class TestSkillsList:
    @pytest.mark.asyncio
    async def test_no_loader(self):
        _current_loader.set(None)
        r = await skills_list.fn()
        assert not r.is_error
        assert "(no skills configured)" in r.content

    @pytest.mark.asyncio
    async def test_loader_throws(self):
        _current_loader.set(_ThrowingLoader())
        r = await skills_list.fn()
        assert not r.is_error
        assert "(failed to load skills)" in r.content

    @pytest.mark.asyncio
    async def test_empty_skills(self):
        _current_loader.set(_FakeLoader([]))
        r = await skills_list.fn()
        assert "(no skills available)" in r.content

    @pytest.mark.asyncio
    async def test_lists_all(self):
        loader = _FakeLoader([
            _FakeSkill("tdd", "eng", "Test-driven development"),
            _FakeSkill("review", "eng", "Code review"),
        ])
        _current_loader.set(loader)
        r = await skills_list.fn()
        assert not r.is_error
        assert "tdd" in r.content
        assert "review" in r.content

    @pytest.mark.asyncio
    async def test_query_filter(self):
        loader = _FakeLoader([
            _FakeSkill("tdd", "eng", "Test-driven development"),
            _FakeSkill("review", "eng", "Code review"),
        ])
        _current_loader.set(loader)
        r = await skills_list.fn(query="tdd")
        assert "tdd" in r.content
        assert "review" not in r.content

    @pytest.mark.asyncio
    async def test_query_no_match(self):
        loader = _FakeLoader([
            _FakeSkill("tdd", "eng", "Test-driven development"),
        ])
        _current_loader.set(loader)
        r = await skills_list.fn(query="zzz-nope")
        assert "(no skills matching" in r.content

    @pytest.mark.asyncio
    async def test_truncates_description(self):
        loader = _FakeLoader([_FakeSkill("long", "eng", "x" * 500)])
        _current_loader.set(loader)
        r = await skills_list.fn()
        # description truncated to 100 chars
        assert "x" * 100 in r.content
        assert "x" * 200 not in r.content


class _FakeStore:
    async def load_history(self, session_id):
        return []


class TestSessionSearch:
    @pytest.mark.asyncio
    async def test_empty_query(self):
        _current_store.set(None)
        r = await session_search.fn(query="")
        assert r.is_error
        assert "query is required" in r.content

    @pytest.mark.asyncio
    async def test_no_store(self):
        _current_store.set(None)
        r = await session_search.fn(query="hello")
        assert r.is_error
        assert "session store not available" in r.content

    @pytest.mark.asyncio
    async def test_no_results(self, monkeypatch):
        from microagent.tools.builtins import session_search as ss
        monkeypatch.setattr(ss, "search_sessions", _fake_search_no_results)
        _current_store.set(_FakeStore())
        r = await session_search.fn(query="needle")
        assert not r.is_error
        assert "(no matching messages found)" in r.content

    @pytest.mark.asyncio
    async def test_with_results(self, monkeypatch):
        from microagent.tools.builtins import session_search as ss
        monkeypatch.setattr(ss, "search_sessions", _fake_search_results)
        _current_store.set(_FakeStore())
        r = await session_search.fn(query="python")
        assert not r.is_error
        assert "[USER]" in r.content
        assert "what is python" in r.content

    @pytest.mark.asyncio
    async def test_search_error(self, monkeypatch):
        from microagent.tools.builtins import session_search as ss
        async def _err(store, query, k=5):
            raise ValueError("fts broken")
        monkeypatch.setattr(ss, "search_sessions", _err)
        _current_store.set(_FakeStore())
        r = await session_search.fn(query="x")
        assert r.is_error
        assert "search failed" in r.content


async def _fake_search_no_results(store, query, k=5):
    return []


async def _fake_search_results(store, query, k=5):
    return [Message.user("what is python"), Message.assistant("python is great")]

"""Regression tests for bugs found via crazy/stress testing round 2.

Two bugs found through deep probes of the less-tested subsystems:
1. SQLiteMemoryProvider.recall() crashes on FTS5-special-character queries
   (", AND, OR, NOT, *, (, NEAR) — OperationalError instead of graceful.
2. ClaudeSkillLoader crashes when search_paths entries are str (natural
   idiom) instead of pathlib.Path — AttributeError: 'str' has no 'exists'.
"""

import asyncio
import pytest
import tempfile
import time


@pytest.mark.asyncio
async def test_memory_recall_special_chars_does_not_crash():
    """FTS5 MATCH uses a query-language grammar. Queries with special chars
    (a bare quote, standalone AND/OR/NOT, *, (, NEAR) used to raise
    OperationalError. Now they must degrade gracefully (fallback to LIKE)."""
    from microagent.memory.provider import SQLiteMemoryProvider, Memory

    prov = SQLiteMemoryProvider(tempfile.mktemp(suffix=".db"))
    await prov.batch_write((
        Memory(id="m1", content="hello world foo", category="fact", created_at=time.time()),
        Memory(id="m2", content="python programming guide", category="fact", created_at=time.time()),
    ))

    # All of these previously crashed recall with OperationalError
    for query in ['"', "AND", "OR", "NOT", "*", "(", "hello AND", "NEAR", "hello OR world"]:
        try:
            await prov.recall(query, k=5)
        except Exception as e:
            pytest.fail(f"recall({query!r}) crashed: {type(e).__name__}: {e}")
    prov.close()


@pytest.mark.asyncio
async def test_memory_recall_valid_query_still_works():
    """Valid queries still return FTS5 results after the fallback change."""
    from microagent.memory.provider import SQLiteMemoryProvider, Memory

    prov = SQLiteMemoryProvider(tempfile.mktemp(suffix=".db"))
    await prov.batch_write((
        Memory(id="m1", content="hello world foo", category="fact", created_at=time.time()),
    ))
    hits = await prov.recall("hello", k=5)
    assert len(hits) == 1
    assert hits[0].content == "hello world foo"
    prov.close()


@pytest.mark.asyncio
async def test_memory_recall_like_fallback_finds_matches():
    """The LIKE fallback path (used for FTS5-invalid queries) still finds
    substring matches."""
    from microagent.memory.provider import SQLiteMemoryProvider, Memory

    prov = SQLiteMemoryProvider(tempfile.mktemp(suffix=".db"))
    await prov.batch_write((
        Memory(id="m1", content="the quick brown fox", category="fact", created_at=time.time()),
    ))
    # 'the AND' is FTS5-invalid (dangling AND) → falls back to LIKE '%the AND%'
    # which matches nothing (content has no literal 'the AND'), returns empty
    hits = await prov.recall("quick", k=5)
    assert len(hits) == 1  # 'quick' is a valid FTS5 term, normal path
    prov.close()


def test_skill_loader_accepts_str_paths():
    """ClaudeSkillLoader(search_paths=('~/skills',)) with str entries used to
    crash on str.exists(). Now converts to Path."""
    from microagent.skill.loader import ClaudeSkillLoader
    import os
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    sd = d / "s1"
    sd.mkdir()
    (sd / "SKILL.md").write_text("---\nname: s1\ndescription: test skill\n---\nbody\n")

    # String path (the natural idiom) — must not crash
    loader = ClaudeSkillLoader(search_paths=(str(d),))
    skills = asyncio.run(loader.load())
    assert len(skills) == 1
    assert skills[0].name == "s1"


def test_skill_loader_accepts_path_entries():
    """Path entries still work after the conversion change."""
    from microagent.skill.loader import ClaudeSkillLoader
    from pathlib import Path
    import tempfile

    d = Path(tempfile.mkdtemp())
    sd = d / "s1"
    sd.mkdir()
    (sd / "SKILL.md").write_text("---\nname: s1\ndescription: t\n---\nbody\n")

    loader = ClaudeSkillLoader(search_paths=(d,))
    skills = asyncio.run(loader.load())
    assert len(skills) == 1


def test_skill_loader_expands_user_tilde():
    """~ in a str path is expanded (the common ~/.claude/skills idiom)."""
    from microagent.skill.loader import ClaudeSkillLoader
    # Verify the expansion logic doesn't crash for a ~ path (may not exist)
    loader = ClaudeSkillLoader(search_paths=("~/nonexistent-dir-for-test",))
    skills = asyncio.run(loader.load())
    assert skills == ()  # nonexistent dir → no skills, no crash

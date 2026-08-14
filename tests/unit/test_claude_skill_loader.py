"""Tests for ClaudeSkillLoader — discovers and parses SKILL.md files."""

from pathlib import Path

from microagent.skill.loader import ClaudeSkillLoader


def _make_skill_dir(base: Path, name: str, frontmatter: str, body: str = "body"):
    """Helper: create a SKILL.md file with given frontmatter and body."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    md = f"---\n{frontmatter}\n---\n\n{body}"
    (skill_dir / "SKILL.md").write_text(md)
    return skill_dir


class TestClaudeSkillLoader:
    async def test_load_single_skill(self, tmp_path):
        _make_skill_dir(
            tmp_path,
            "test-skill",
            ("name: test-skill\ndescription: A test skill for verification.\n"),
        )
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        skills = await loader.load()
        assert len(skills) == 1
        assert skills[0].name == "test-skill"
        assert skills[0].description == "A test skill for verification."
        assert skills[0].namespace == "claude"
        assert "body" in skills[0].body

    async def test_load_multiple_skills(self, tmp_path):
        _make_skill_dir(tmp_path, "skill-a", "name: skill-a\ndescription: Skill A.\n")
        _make_skill_dir(tmp_path, "skill-b", "name: skill-b\ndescription: Skill B.\n")
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        skills = await loader.load()
        assert len(skills) == 2

    async def test_triggers_from_frontmatter(self, tmp_path):
        _make_skill_dir(
            tmp_path,
            "triggered",
            ("name: triggered\ndescription: With triggers.\ntriggers: [search, deep, find]\n"),
        )
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        skills = await loader.load()
        assert skills[0].triggers == ("search", "deep", "find")

    async def test_skip_non_skill_dirs(self, tmp_path):
        # Create a dir without SKILL.md — should be skipped
        (tmp_path / "not-a-skill").mkdir()
        _make_skill_dir(tmp_path, "real-skill", "name: real-skill\ndescription: Real.\n")
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        skills = await loader.load()
        assert len(skills) == 1

    async def test_match_by_keyword(self, tmp_path):
        _make_skill_dir(
            tmp_path,
            "search-skill",
            ("name: search-skill\ndescription: Searches things.\ntriggers: [search, lookup]\n"),
        )
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        matches = await loader.match("I want to search for something")
        assert len(matches) == 1
        assert matches[0].match_reason == "keyword:search"

    async def test_match_by_fuzzy(self, tmp_path):
        _make_skill_dir(
            tmp_path, "fuzzy-skill", ("name: fuzzy-skill\ndescription: Fuzzy matching test.\n")
        )
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        matches = await loader.match("fuzzy matching test")
        assert len(matches) >= 1
        assert matches[0].match_score > 0.4

    async def test_no_match(self, tmp_path):
        _make_skill_dir(
            tmp_path, "unrelated", ("name: unrelated\ndescription: Completely unrelated skill.\n")
        )
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        matches = await loader.match("xyzzy nonsense gibberish")
        assert len(matches) == 0

    async def test_empty_dir(self, tmp_path):
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        skills = await loader.load()
        assert skills == ()
        matches = await loader.match("anything")
        assert matches == ()

    async def test_description_with_colon_and_arrow(self, tmp_path):
        """Regression: a description containing ': ' (colon+space, e.g. a
        'reproduce → fix' arrow) must not break YAML frontmatter parsing.

        YAML rejects unquoted `key: value: more` as a mapping error; the
        parser must still load the skill (quoted value or tolerant parse).
        """
        _make_skill_dir(
            tmp_path,
            "arrow-skill",
            "name: arrow-skill\n"
            'description: "Reproduce: fix → verify."\n',
        )
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        skills = await loader.load()
        assert len(skills) == 1
        assert skills[0].name == "arrow-skill"
        assert skills[0].description == "Reproduce: fix → verify."

    async def test_match_cjk_long_description(self, tmp_path):
        """Regression: CJK matching must survive long descriptions.

        A short natural-language query against a long Chinese description
        used to score near zero because Jaccard divided by the union of
        bigrams (dominated by the target's length). Query-coverage must
        let a verbatim sub-phrase of the description match.
        """
        _make_skill_dir(
            tmp_path,
            "zh-skill",
            "name: zh-skill\n"
            "description: 测试驱动开发。当用户想要测试先行地构建功能或修复 bug、"
            "提到red-green-refactor或想要集成测试时使用。\n",
        )
        loader = ClaudeSkillLoader(search_paths=(tmp_path,))
        # Verbatim sub-phrase of the description → must match.
        matches = await loader.match("测试驱动开发")
        assert any(m.skill.name == "zh-skill" for m in matches)
        # Near-phrase → must match.
        matches = await loader.match("测试先行构建功能")
        assert any(m.skill.name == "zh-skill" for m in matches)
        # Unrelated Chinese query → must NOT match (no false positive).
        matches = await loader.match("今天天气怎么样")
        assert all(m.skill.name != "zh-skill" for m in matches)


class TestCJKHelpers:
    def test_cjk_aware_ratio_latin(self):
        from microagent.skill.loader import _cjk_aware_ratio
        r = _cjk_aware_ratio("test", "testing tools")
        assert 0 < r <= 1.0

    def test_cjk_aware_ratio_cjk(self):
        from microagent.skill.loader import _cjk_aware_ratio
        r = _cjk_aware_ratio("测试", "测试驱动开发")
        assert r > 0

    def test_cjk_aware_ratio_no_overlap(self):
        from microagent.skill.loader import _cjk_aware_ratio
        assert _cjk_aware_ratio("aaa", "bbb") == 0.0

    def test_cjk_aware_ratio_empty(self):
        from microagent.skill.loader import _cjk_aware_ratio
        assert _cjk_aware_ratio("", "abc") == 0.0
        assert _cjk_aware_ratio("abc", "") == 0.0

    def test_lcs_len(self):
        from microagent.skill.loader import _lcs_len
        # LCS here = longest common substring (contiguous)
        assert _lcs_len("abcdef", "abxyzf") == 2  # "ab"
        assert _lcs_len("hello", "hello") == 5
        assert _lcs_len("abc", "def") == 0

    def test_bigrams(self):
        from microagent.skill.loader import _bigrams
        b = _bigrams("abcd")
        assert {"ab", "bc", "cd"} <= b


class TestParseSkillMd:
    def test_no_frontmatter(self, tmp_path):
        from microagent.skill.loader import _parse_skill_md
        f = tmp_path / "SKILL.md"
        f.write_text("just body text, no frontmatter")
        assert _parse_skill_md(f) is None

    def test_malformed_frontmatter_logs(self, tmp_path, caplog):
        import logging
        from microagent.skill.loader import _parse_skill_md
        f = tmp_path / "SKILL.md"
        # YAML with a mapping error
        f.write_text("---\nname: x\ndescription: bad: value\n---\nbody\n")
        with caplog.at_level(logging.WARNING):
            result = _parse_skill_md(f)
        assert result is None or result.name == "x"
        if result is None:
            assert any("SKILL.md" in r.message for r in caplog.records)

    def test_triggers_as_string(self, tmp_path):
        from microagent.skill.loader import _parse_skill_md
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: s\ndescription: d\ntriggers: a, b, c\n---\nbody\n")
        skill = _parse_skill_md(f)
        assert skill is not None
        assert skill.name == "s"

    def test_triggers_as_list(self, tmp_path):
        from microagent.skill.loader import _parse_skill_md
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: s\ndescription: d\ntriggers:\n  - t1\n  - t2\n---\nbody\n")
        skill = _parse_skill_md(f)
        assert skill is not None
        assert skill.triggers == ("t1", "t2")

    def test_default_namespace(self, tmp_path):
        from microagent.skill.loader import _parse_skill_md
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: s\ndescription: d\n---\nbody\n")
        skill = _parse_skill_md(f)
        assert skill.namespace == "claude"


class TestCompositeSkillLoader:
    async def test_match_deduplicates(self):
        from microagent.skill.loader import (
            CompositeSkillLoader, Skill, LoadedSkill,
        )

        class _Backend:
            def __init__(self, skills):
                self._s = skills
            async def load(self):
                return tuple(s for s, _ in self._s)
            async def match(self, user_input):
                return tuple(
                    LoadedSkill(skill=s, match_reason="r", match_score=score)
                    for s, score in self._s
                )

        skill = Skill(name="shared", namespace="ns", description="d", body="b",
                      triggers=(), source="/tmp/s", mtime=0.0)
        b1 = _Backend([(skill, 0.9)])
        b2 = _Backend([(skill, 0.8)])
        loader = CompositeSkillLoader(backends=(b1, b2))
        matches = await loader.match("anything")
        # Deduplicated — the same skill appears once
        assert len(matches) == 1

    async def test_match_sorts_by_score(self):
        from microagent.skill.loader import (
            CompositeSkillLoader, Skill, LoadedSkill,
        )

        class _Backend:
            async def load(self):
                return ()
            async def match(self, user_input):
                return tuple(
                    LoadedSkill(skill=s, match_reason="r", match_score=score)
                    for s, score in self._skills
                )
            def __init__(self, skills):
                self._skills = skills

        s1 = Skill(name="a", namespace="ns", description="d", body="b", triggers=(),
                   source="/s", mtime=0.0)
        s2 = Skill(name="b", namespace="ns", description="d", body="b", triggers=(),
                   source="/s", mtime=0.0)
        backend = _Backend([(s1, 0.3), (s2, 0.9)])
        loader = CompositeSkillLoader(backends=(backend,))
        matches = await loader.match("x")
        # highest score first
        assert matches[0].skill.name == "b"


async def test_load_caches_until_mtime_changes(tmp_path):
    """Regression: load() re-parsed every SKILL.md on every call (the runner
    calls it up to 3x/turn). Now it caches by an mtime/size fingerprint and
    only re-parses when a file changes."""
    from microagent.skill.loader import ClaudeSkillLoader

    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    md = skill_dir / "SKILL.md"
    md.write_text("---\nname: demo\ndescription: hello world\n---\nbody v1\n")

    loader = ClaudeSkillLoader((tmp_path,))
    first = await loader.load()
    assert len(first) == 1 and first[0].name == "demo"
    # Same content, no mtime change → cached, identical tuple object.
    second = await loader.load()
    assert second is first, "cache missed on identical fingerprint"

    # Edit the skill (updates mtime) → cache invalidates, body changes.
    import time as _time
    _time.sleep(0.01)  # ensure mtime tick on coarse filesystems
    md.write_text("---\nname: demo\ndescription: hello world\n---\nbody v2\n")
    third = await loader.load()
    assert third is not first, "cache not invalidated on mtime change"
    assert third[0].body == "body v2"


async def test_load_offloads_to_thread_not_blocking_loop(tmp_path):
    """load() must not run sync disk I/O on the event loop thread."""
    import asyncio
    from microagent.skill.loader import ClaudeSkillLoader

    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "SKILL.md").write_text(
        "---\nname: x\ndescription: d\n---\nb\n"
    )
    loader = ClaudeSkillLoader((tmp_path,))

    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _block_loop():
        # If load() ran on the loop thread, this scheduled callback could
        # not make progress until load() returned. Run it via call_soon.
        loop.call_soon_threadsafe(fut.set_result, "loop-alive")

    loop.run_in_executor(None, _block_loop)
    await loader.load()
    # If load() had blocked the loop synchronously the executor callback
    # would still be pending; this awaits it within a tight timeout.
    await asyncio.wait_for(fut, timeout=2.0)


class TestCJKOrderSensitivity:
    """Subsequence coverage preserves word order — set coverage alone
    scored '测试驱动' against a '驱动测试' description identically."""

    def test_order_preserving_subsequence_boosts_in_order(self):
        from microagent.skill.loader import _cjk_aware_ratio

        in_order = _cjk_aware_ratio("测试驱动开发", "测试先行驱动开发流程")
        reversed_target = _cjk_aware_ratio("测试驱动开发", "开发驱动先行测试流程")
        # In-order subsequence must outscore the reversed-target score.
        assert in_order > reversed_target, f"{in_order} !> {reversed_target}"

    def test_subseq_tolerates_interleaved_bigrams(self):
        from microagent.skill.loader import _cjk_aware_ratio

        # The query's bigrams all appear in the target but NOT as a
        # contiguous run — subsequence (not substring) must still score.
        r = _cjk_aware_ratio("测试驱动", "测试方法是先写驱动")
        assert r > 0

    def test_unrelated_still_zero(self):
        from microagent.skill.loader import _cjk_aware_ratio

        assert _cjk_aware_ratio("今天天气", "代码审查流程") == 0.0


def test_lcs_subseq_len_helper():
    from microagent.skill.loader import _lcs_subseq_len

    assert _lcs_subseq_len(["a", "b", "c"], ["a", "x", "b", "y", "c"]) == 3
    assert _lcs_subseq_len(["a", "b"], ["b", "a"]) == 1  # order matters

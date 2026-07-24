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

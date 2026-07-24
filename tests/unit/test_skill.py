"""Tests for Skill data model and SkillLoader Protocol."""

from microagent.skill.loader import LoadedSkill, Skill


class TestSkill:
    def test_create_skill(self):
        s = Skill(
            name="deepsearch",
            namespace="claude",
            description="Search deeply for information.",
            body="# Deep Search\nSearch deep.",
            triggers=("search", "deep"),
            source="~/.claude/skills/deepsearch/SKILL.md",
            mtime=12345.0,
        )
        assert s.name == "deepsearch"
        assert s.namespace == "claude"
        assert s.triggers == ("search", "deep")


class TestLoadedSkill:
    def test_create_loaded(self):
        s = Skill(
            name="test",
            namespace="claude",
            description="test skill",
            body="body",
            triggers=(),
            source="",
            mtime=0.0,
        )
        ls = LoadedSkill(skill=s, match_reason="keyword:search", match_score=1.0)
        assert ls.skill is s
        assert ls.match_reason == "keyword:search"
        assert ls.match_score == 1.0

"""Tests for Curator — background skill lifecycle management."""

import json
import time
from pathlib import Path

from microagent.skill.curator import Curator


class TestCurator:
    def _make_skill_dir(self, base: Path, name: str, created_by: str = "agent") -> Path:
        d = base / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# {name}\ntest skill")
        if created_by == "agent":
            (d / ".provenance.json").write_text(json.dumps({"created_by": "agent"}))
        return d

    def _make_usage(self, usage_file: Path, data: dict) -> None:
        usage_file.parent.mkdir(parents=True, exist_ok=True)
        usage_file.write_text(json.dumps(data))

    async def test_skips_non_agent_skills(self, tmp_path):
        """Curator only touches skills with created_by='agent'."""
        skills_dir = tmp_path / "skills"
        self._make_skill_dir(skills_dir, "agent-skill", "agent")
        self._make_skill_dir(skills_dir, "user-skill", "user")

        usage_file = tmp_path / ".usage.json"
        now = time.time()
        self._make_usage(
            usage_file,
            {
                "agent-skill": {
                    "use_count": 0,
                    "last_activity": now - 100 * 86400,
                    "state": "active",
                    "pinned": False,
                },
                "user-skill": {
                    "use_count": 0,
                    "last_activity": now - 100 * 86400,
                    "state": "active",
                    "pinned": False,
                },
            },
        )

        curator = Curator(stale_after_days=30.0, archive_after_days=90.0)
        await curator.run_once(skills_dir, usage_file)

        # Agent skill should become stale (100 days > 30)
        usage = json.loads(usage_file.read_text())
        assert usage["agent-skill"]["state"] == "stale"
        # User skill should NOT be touched
        assert usage["user-skill"]["state"] == "active"

    async def test_stale_to_archived(self, tmp_path):
        """Skill idle for 90+ days moves from stale to archived."""
        skills_dir = tmp_path / "skills"
        self._make_skill_dir(skills_dir, "old-skill", "agent")

        usage_file = tmp_path / ".usage.json"
        now = time.time()
        self._make_usage(
            usage_file,
            {
                "old-skill": {
                    "use_count": 0,
                    "last_activity": now - 100 * 86400,
                    "state": "stale",
                    "pinned": False,
                },
            },
        )

        curator = Curator(stale_after_days=30.0, archive_after_days=90.0)
        await curator.run_once(skills_dir, usage_file)

        # Skill should be archived (moved to .archive/)
        assert not (skills_dir / "old-skill").exists()
        assert (skills_dir / ".archive" / "old-skill").exists()
        usage = json.loads(usage_file.read_text())
        assert usage["old-skill"]["state"] == "archived"

    async def test_pinned_skill_untouched(self, tmp_path):
        """Pinned skills are never archived, regardless of idle time."""
        skills_dir = tmp_path / "skills"
        self._make_skill_dir(skills_dir, "pinned-skill", "agent")

        usage_file = tmp_path / ".usage.json"
        now = time.time()
        self._make_usage(
            usage_file,
            {
                "pinned-skill": {
                    "use_count": 10,
                    "last_activity": now - 200 * 86400,
                    "state": "active",
                    "pinned": True,
                },
            },
        )

        curator = Curator(stale_after_days=30.0, archive_after_days=90.0)
        await curator.run_once(skills_dir, usage_file)

        # Pinned skill should still be active
        usage = json.loads(usage_file.read_text())
        assert usage["pinned-skill"]["state"] == "active"
        assert (skills_dir / "pinned-skill").exists()

    async def test_empty_dir(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        usage_file = tmp_path / ".usage.json"
        self._make_usage(usage_file, {})

        curator = Curator()
        await curator.run_once(skills_dir, usage_file)
        # Should not crash

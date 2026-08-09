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


class TestCuratorMore:
    async def test_archive_with_existing_dest(self, tmp_path):
        """Archiving a skill whose .archive dest already exists overwrites it."""
        skills_dir = tmp_path / "skills"
        d = skills_dir / "myskill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("body")
        (d / ".provenance.json").write_text('{"created_by": "agent"}')

        archive = skills_dir / ".archive"
        archive.mkdir(exist_ok=True)
        (archive / "myskill").mkdir()
        (archive / "myskill" / "SKILL.md").write_text("old")

        curator = Curator()
        curator._archive(d)
        # The archive dest was replaced
        assert (archive / "myskill" / "SKILL.md").read_text() == "body"

    async def test_is_agent_created_bad_json(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / ".provenance.json").write_text("{not valid json")
        assert Curator._is_agent_created(d) is False

    async def test_is_agent_created_missing_file(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        assert Curator._is_agent_created(d) is False

    async def test_is_agent_created_not_agent(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / ".provenance.json").write_text('{"created_by": "user"}')
        assert Curator._is_agent_created(d) is False

    async def test_load_usage_missing(self, tmp_path):
        assert Curator._load_usage(tmp_path / "missing.json") == {}

    async def test_load_usage_corrupt(self, tmp_path):
        f = tmp_path / "usage.json"
        f.write_text("{corrupt")
        assert Curator._load_usage(f) == {}

    async def test_load_usage_valid(self, tmp_path):
        f = tmp_path / "usage.json"
        f.write_text('{"s1": {"state": "active"}}')
        assert Curator._load_usage(f) == {"s1": {"state": "active"}}

    async def test_run_once_skips_non_dir_and_hidden(self, tmp_path):
        """run_once ignores files and hidden dirs in the skills dir."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "somefile.txt").write_text("not a dir")
        (skills_dir / ".hidden").mkdir()
        usage_file = tmp_path / "usage.json"
        usage_file.write_text("{}")
        curator = Curator()
        await curator.run_once(skills_dir, usage_file)  # must not crash

    async def test_run_once_skips_no_entry_or_pinned(self, tmp_path):
        """Skills with no usage entry or pinned=True are untouched."""
        from microagent.skill.curator import Curator
        skills_dir = tmp_path / "skills"
        # skill with no usage entry
        d1 = skills_dir / "noentry"
        d1.mkdir(parents=True)
        (d1 / ".provenance.json").write_text('{"created_by": "agent"}')
        # pinned skill
        d2 = skills_dir / "pinned"
        d2.mkdir(parents=True)
        (d2 / ".provenance.json").write_text('{"created_by": "agent"}')

        now = time.time()
        usage_file = tmp_path / "usage.json"
        usage_file.write_text(json.dumps({
            "pinned": {"state": "active", "pinned": True, "last_activity": now - 100 * 86400},
        }))
        curator = Curator(stale_after_days=30, archive_after_days=90)
        await curator.run_once(skills_dir, usage_file)
        # both skills remain in place (not archived)
        assert d1.exists()
        assert d2.exists()

    async def test_save_usage_writes_file(self, tmp_path):
        usage_file = tmp_path / "sub" / "usage.json"
        Curator._save_usage(usage_file, {"s1": {"state": "active"}})
        assert usage_file.exists()
        assert json.loads(usage_file.read_text()) == {"s1": {"state": "active"}}


class TestCuratorEdge:
    async def test_run_once_missing_skills_dir(self, tmp_path):
        """A nonexistent skills_dir must be a no-op, not FileNotFoundError."""
        from microagent.skill.curator import Curator

        curator = Curator()
        await curator.run_once(tmp_path / "nonexistent", tmp_path / "usage.json")

    async def test_lsp_dead_client_evicted(self, tmp_path, monkeypatch):
        """_get_client evicts a cached client whose server process died —
        previously it stayed cached forever and every request timed out."""
        from microagent.tools.builtins import lsp as lsp_mod

        state = lsp_mod._get_state()
        dead = type("DeadClient", (), {"_proc": type("P", (), {"returncode": 1})()})()
        state.clients["python"] = dead

        class _FakeClient:
            def __init__(self, cmd, root_uri):
                self._proc = type("P", (), {"returncode": None})()

            async def start(self):
                return None

        monkeypatch.setattr(lsp_mod, "_find_lsp_command", lambda lang: ("fake-lsp",))
        monkeypatch.setattr(lsp_mod, "LSPClient", _FakeClient)
        result = await lsp_mod._get_client(str(tmp_path / "x.py"))
        # Corpse evicted, a NEW client was created and cached
        assert result is not dead
        assert state.clients["python"] is result

"""Tests for skill_manage builtin tool — runtime skill creation/modification."""

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


class TestSkillManage:
    async def test_create_skill(self, tmp_path, monkeypatch):
        """skill_manage creates a SKILL.md file."""
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(
            id="c1",
            name="skill_manage",
            arguments={
                "action": "create",
                "name": "my-skill",
                "content": "# My Skill\nThis is a test skill.",
            },
        )
        result = await registry.execute(call)
        assert not result.is_error
        skill_path = tmp_path / "my-skill" / "SKILL.md"
        assert skill_path.exists()
        content = skill_path.read_text()
        assert "# My Skill" in content

    async def test_list_skills(self, tmp_path, monkeypatch):
        """skill_manage lists agent-created skills."""
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        registry = ToolRegistry(_default_builtins())
        # Create a skill first
        await registry.execute(
            ToolCall(
                id="c1",
                name="skill_manage",
                arguments={
                    "action": "create",
                    "name": "skill-a",
                    "content": "skill a",
                },
            )
        )
        # List
        result = await registry.execute(
            ToolCall(
                id="c2",
                name="skill_manage",
                arguments={
                    "action": "list",
                },
            )
        )
        assert not result.is_error
        assert "skill-a" in result.content

    async def test_patch_skill(self, tmp_path, monkeypatch):
        """skill_manage patches an existing skill."""
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        registry = ToolRegistry(_default_builtins())
        # Create
        await registry.execute(
            ToolCall(
                id="c1",
                name="skill_manage",
                arguments={
                    "action": "create",
                    "name": "patch-me",
                    "content": "original content",
                },
            )
        )
        # Patch
        result = await registry.execute(
            ToolCall(
                id="c2",
                name="skill_manage",
                arguments={
                    "action": "patch",
                    "name": "patch-me",
                    "old_string": "original content",
                    "new_string": "patched content",
                },
            )
        )
        assert not result.is_error
        content = (tmp_path / "patch-me" / "SKILL.md").read_text()
        assert "patched content" in content

    async def test_delete_skill(self, tmp_path, monkeypatch):
        """skill_manage deletes a skill directory."""
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        registry = ToolRegistry(_default_builtins())
        await registry.execute(
            ToolCall(
                id="c1",
                name="skill_manage",
                arguments={
                    "action": "create",
                    "name": "delete-me",
                    "content": "bye",
                },
            )
        )
        result = await registry.execute(
            ToolCall(
                id="c2",
                name="skill_manage",
                arguments={
                    "action": "delete",
                    "name": "delete-me",
                },
            )
        )
        assert not result.is_error
        assert not (tmp_path / "delete-me").exists()

    async def test_patch_user_created_skill_rejected(self, tmp_path, monkeypatch):
        """patch has the same provenance guard as delete — skill bodies flow
        into the system prompt, so rewriting a user skill is a persistent
        prompt-injection channel."""
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        # Simulate a user-written skill (no provenance file)
        (tmp_path / "user-skill").mkdir()
        (tmp_path / "user-skill" / "SKILL.md").write_text("user instructions")
        registry = ToolRegistry(_default_builtins())
        result = await registry.execute(
            ToolCall(
                id="c1",
                name="skill_manage",
                arguments={
                    "action": "patch",
                    "name": "user-skill",
                    "old_string": "user instructions",
                    "new_string": "INJECTED",
                },
            )
        )
        assert result.is_error
        assert "not created by an agent" in result.content
        assert (tmp_path / "user-skill" / "SKILL.md").read_text() == "user instructions"

    async def test_create_does_not_overwrite_user_skill(self, tmp_path, monkeypatch):
        """create on an existing user-written skill must refuse to overwrite."""
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        (tmp_path / "user-skill").mkdir()
        (tmp_path / "user-skill" / "SKILL.md").write_text("user instructions")
        registry = ToolRegistry(_default_builtins())
        result = await registry.execute(
            ToolCall(
                id="c1",
                name="skill_manage",
                arguments={
                    "action": "create",
                    "name": "user-skill",
                    "content": "INJECTED",
                },
            )
        )
        assert result.is_error
        assert "not created by an agent" in result.content
        assert (tmp_path / "user-skill" / "SKILL.md").read_text() == "user instructions"

    async def test_create_can_overwrite_agent_skill(self, tmp_path, monkeypatch):
        """create on an agent-created skill is allowed (idempotent rewrite)."""
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        registry = ToolRegistry(_default_builtins())
        await registry.execute(
            ToolCall(
                id="c1",
                name="skill_manage",
                arguments={"action": "create", "name": "mine", "content": "v1"},
            )
        )
        result = await registry.execute(
            ToolCall(
                id="c2",
                name="skill_manage",
                arguments={"action": "create", "name": "mine", "content": "v2"},
            )
        )
        assert not result.is_error
        assert (tmp_path / "mine" / "SKILL.md").read_text() == "v2"

    async def test_invalid_action(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        registry = ToolRegistry(_default_builtins())
        result = await registry.execute(
            ToolCall(
                id="c1",
                name="skill_manage",
                arguments={
                    "action": "unknown",
                    "name": "x",
                },
            )
        )
        assert result.is_error


class TestSkillManageMore:
    async def test_create_too_large(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        r = await sm.skill_manage.fn(action="create", name="big", content="x" * 2_000_000)
        assert r.is_error
        assert "too large" in r.content

    async def test_create_missing_fields(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        r = await sm.skill_manage.fn(action="create", name="", content="")
        assert r.is_error
        assert "name and content" in r.content

    async def test_patch_missing_name_or_old(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        r = await sm.skill_manage.fn(action="patch", name="s", old_string="")
        assert r.is_error

    async def test_patch_not_found(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        r = await sm.skill_manage.fn(action="patch", name="nope", old_string="x", new_string="y")
        assert r.is_error
        assert "not found" in r.content

    async def test_patch_old_not_found(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        sd = tmp_path / "s1"
        sd.mkdir()
        (sd / "SKILL.md").write_text("hello world")
        (sd / ".provenance.json").write_text('{"created_by": "agent"}')
        r = await sm.skill_manage.fn(action="patch", name="s1", old_string="zzz", new_string="yyy")
        assert r.is_error
        assert "not found" in r.content

    async def test_patch_multi_match(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        sd = tmp_path / "s1"
        sd.mkdir()
        (sd / "SKILL.md").write_text("dup dup dup")
        (sd / ".provenance.json").write_text('{"created_by": "agent"}')
        r = await sm.skill_manage.fn(action="patch", name="s1", old_string="dup", new_string="x")
        assert r.is_error
        assert "matches 3 times" in r.content

    async def test_list_no_skills_dir(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path / "nonexistent")
        r = await sm.skill_manage.fn(action="list")
        assert not r.is_error
        assert "(no skills)" in r.content

    async def test_list_no_agent_skills(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        sd = tmp_path / "user-skill"
        sd.mkdir()
        (sd / "SKILL.md").write_text("body")
        (sd / ".provenance.json").write_text('{"created_by": "user"}')
        r = await sm.skill_manage.fn(action="list")
        assert not r.is_error
        assert "(no agent-created skills)" in r.content

    async def test_delete_not_found(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        r = await sm.skill_manage.fn(action="delete", name="nope")
        assert r.is_error
        assert "not found" in r.content

    async def test_delete_missing_name(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        r = await sm.skill_manage.fn(action="delete", name="")
        assert r.is_error

    async def test_delete_not_agent_created(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        sd = tmp_path / "user-skill"
        sd.mkdir()
        (sd / "SKILL.md").write_text("body")
        (sd / ".provenance.json").write_text('{"created_by": "user"}')
        r = await sm.skill_manage.fn(action="delete", name="user-skill")
        assert r.is_error
        assert "not created by an agent" in r.content

    async def test_is_agent_created_bad_json(self, tmp_path, monkeypatch):
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        sd = tmp_path / "bad"
        sd.mkdir()
        (sd / ".provenance.json").write_text("{bad json")
        assert sm._is_agent_created("bad") is False

    async def test_touch_curator_usage(self, tmp_path, monkeypatch):
        import json as _json
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        # First touch creates the file
        sm._touch_curator_usage("s1")
        usage_file = tmp_path / ".usage.json"
        assert usage_file.exists()
        data = _json.loads(usage_file.read_text())
        assert data["s1"]["use_count"] == 1
        assert data["s1"]["state"] == "active"
        # Second touch increments
        sm._touch_curator_usage("s1")
        data = _json.loads(usage_file.read_text())
        assert data["s1"]["use_count"] == 2

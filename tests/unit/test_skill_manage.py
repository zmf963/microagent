"""Tests for skill_manage builtin tool — runtime skill creation/modification."""

import pytest
from pathlib import Path
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
        call = ToolCall(id="c1", name="skill_manage", arguments={
            "action": "create",
            "name": "my-skill",
            "content": "# My Skill\nThis is a test skill.",
        })
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
        await registry.execute(ToolCall(id="c1", name="skill_manage", arguments={
            "action": "create", "name": "skill-a", "content": "skill a",
        }))
        # List
        result = await registry.execute(ToolCall(id="c2", name="skill_manage", arguments={
            "action": "list",
        }))
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
        await registry.execute(ToolCall(id="c1", name="skill_manage", arguments={
            "action": "create", "name": "patch-me", "content": "original content",
        }))
        # Patch
        result = await registry.execute(ToolCall(id="c2", name="skill_manage", arguments={
            "action": "patch", "name": "patch-me",
            "old_string": "original content",
            "new_string": "patched content",
        }))
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
        await registry.execute(ToolCall(id="c1", name="skill_manage", arguments={
            "action": "create", "name": "delete-me", "content": "bye",
        }))
        result = await registry.execute(ToolCall(id="c2", name="skill_manage", arguments={
            "action": "delete", "name": "delete-me",
        }))
        assert not result.is_error
        assert not (tmp_path / "delete-me").exists()

    async def test_invalid_action(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "microagent.tools.builtins.skill_manage._get_skills_dir",
            lambda: tmp_path,
        )
        registry = ToolRegistry(_default_builtins())
        result = await registry.execute(ToolCall(id="c1", name="skill_manage", arguments={
            "action": "unknown", "name": "x",
        }))
        assert result.is_error

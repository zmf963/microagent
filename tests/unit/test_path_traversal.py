"""Regression tests for path-traversal hardening.

skill_manage previously joined an LLM-supplied `name` into a filesystem
path with zero validation: name="../../../etc/cron.d/evil" would let an
agent (or a prompt-injected LLM) write a SKILL.md anywhere on disk, or
delete arbitrary directories via the 'delete' action + shutil.rmtree.

ToolOutputStore had the same hole via tool_call_id / session_id.

These tests verify the rejection paths without touching the real skills
directory — they monkeypatch _get_skills_dir to a tmp_path.
"""

from pathlib import Path

import pytest

from microagent.tools.builtins.skill_manage import is_safe_name
from microagent.tools.output_store import ToolOutputStore


async def _call_skill(action: str, name: str = "", content: str = "",
                     old_string: str = "", new_string: str = ""):
    """Invoke the skill_manage tool function directly with given args."""
    from microagent.tools.builtins.skill_manage import skill_manage
    return await skill_manage.fn(
        action=action, name=name, content=content,
        old_string=old_string, new_string=new_string,
    )


class TestSafeName:
    @pytest.mark.parametrize("name", ["my-skill", "my_skill", "Skill1", "a.b.c", "tdd"])
    def test_accepts_valid(self, name):
        assert is_safe_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "..",
            ".",
            "../etc/cron.d/evil",
            "/etc/passwd",
            "a/b",
            "a\\b",
            "",
            "..hidden",
            "a..b",
            " name",
            "name ",
            "na me",
            "na$me",
            "na;me",
        ],
    )
    def test_rejects_traversal(self, name):
        assert is_safe_name(name) is False, name


@pytest.mark.asyncio
async def test_skill_manage_create_rejects_traversal(tmp_path, monkeypatch):
    """create with a path-traversal name returns an error and writes nothing."""
    from microagent.tools.builtins import skill_manage as sm

    monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
    result = await _call_skill(action="create", name="../../../etc/cron.d/evil", content="# evil")
    assert result.is_error
    assert "invalid skill name" in result.content.lower()
    # Nothing was written outside tmp_path
    assert not (tmp_path.parent.parent.parent / "etc").exists()


@pytest.mark.asyncio
async def test_skill_manage_delete_rejects_traversal(tmp_path, monkeypatch):
    """delete with a traversal name is rejected before rmtree runs."""
    from microagent.tools.builtins import skill_manage as sm

    monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
    result = await _call_skill(action="delete", name="../../important")
    assert result.is_error


@pytest.mark.asyncio
async def test_skill_manage_patch_rejects_traversal(tmp_path, monkeypatch):
    from microagent.tools.builtins import skill_manage as sm

    monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
    result = await _call_skill(
        action="patch", name="../x", old_string="a", new_string="b"
    )
    assert result.is_error


class TestOutputStoreTraversal:
    def test_session_id_traversal_is_hashed(self, tmp_path):
        """A malicious session_id cannot escape base_dir — it's hashed."""
        store = ToolOutputStore(base_dir=tmp_path)
        big = "x" * (store.max_bytes + 100)
        out = store.process(
            tool_call_id="../../etc/cron.d/evil",
            content=big,
            session_id="../../../etc/cron.d/evil2",
        )
        assert out.saved_to_disk
        # File landed inside base_dir, not outside it.
        disk = Path(out.disk_path)
        assert tmp_path in disk.resolve().parents
        assert not (tmp_path.parent.parent / "etc").exists()

    def test_normal_ids_still_persist(self, tmp_path):
        store = ToolOutputStore(base_dir=tmp_path)
        big = "y" * (store.max_bytes + 10)
        out = store.process(
            tool_call_id="call_abc123",
            content=big,
            session_id="sess-1",
        )
        assert out.saved_to_disk
        assert Path(out.disk_path).exists()

"""Tests for M0b-1 builtins: write_file, edit_file, grep, glob, todo, plan, exit."""

import pytest

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


@pytest.fixture
def registry():
    return ToolRegistry(_default_builtins())


class TestWriteFile:
    async def test_write_new_file(self, tmp_path, registry):
        path = str(tmp_path / "new.txt")
        call = ToolCall(id="c1", name="write_file", arguments={"path": path, "content": "hello"})
        result = await registry.execute(call)
        assert not result.is_error
        assert (tmp_path / "new.txt").read_text() == "hello"

    async def test_write_creates_parent_dirs(self, tmp_path, registry):
        path = str(tmp_path / "sub" / "dir" / "file.txt")
        call = ToolCall(id="c1", name="write_file", arguments={"path": path, "content": "nested"})
        result = await registry.execute(call)
        assert not result.is_error
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"

    async def test_overwrite_existing(self, tmp_path, registry):
        path = str(tmp_path / "existing.txt")
        (tmp_path / "existing.txt").write_text("old")
        call = ToolCall(id="c1", name="write_file", arguments={"path": path, "content": "new"})
        await registry.execute(call)
        assert (tmp_path / "existing.txt").read_text() == "new"


class TestEditFile:
    async def test_replace_first(self, tmp_path, registry):
        path = str(tmp_path / "edit.txt")
        (tmp_path / "edit.txt").write_text("foo bar baz bar")
        call = ToolCall(
            id="c1",
            name="edit_file",
            arguments={"path": path, "old_string": "foo", "new_string": "qux"},
        )
        result = await registry.execute(call)
        assert not result.is_error
        assert (tmp_path / "edit.txt").read_text() == "qux bar baz bar"

    async def test_replace_all(self, tmp_path, registry):
        path = str(tmp_path / "edit2.txt")
        (tmp_path / "edit2.txt").write_text("foo bar foo")
        call = ToolCall(
            id="c1",
            name="edit_file",
            arguments={"path": path, "old_string": "foo", "new_string": "baz", "replace_all": True},
        )
        result = await registry.execute(call)
        assert not result.is_error
        assert (tmp_path / "edit2.txt").read_text() == "baz bar baz"

    async def test_replace_ambiguous(self, tmp_path, registry):
        """replace_all=False with multiple matches should error."""
        path = str(tmp_path / "edit_ambig.txt")
        (tmp_path / "edit_ambig.txt").write_text("foo bar foo bar")
        call = ToolCall(
            id="c1",
            name="edit_file",
            arguments={"path": path, "old_string": "foo", "new_string": "baz"},
        )
        result = await registry.execute(call)
        assert result.is_error
        assert "replace_all" in result.content

    async def test_not_found(self, tmp_path, registry):
        path = str(tmp_path / "nofile.txt")
        call = ToolCall(
            id="c1",
            name="edit_file",
            arguments={"path": path, "old_string": "x", "new_string": "y"},
        )
        result = await registry.execute(call)
        assert result.is_error

    async def test_old_string_not_in_file(self, tmp_path, registry):
        path = str(tmp_path / "edit3.txt")
        (tmp_path / "edit3.txt").write_text("hello world")
        call = ToolCall(
            id="c1",
            name="edit_file",
            arguments={"path": path, "old_string": "xyz", "new_string": "abc"},
        )
        result = await registry.execute(call)
        assert result.is_error


class TestGrep:
    async def test_search_file(self, tmp_path, registry):
        f = tmp_path / "code.py"
        f.write_text("import os\nprint('hello')\nx = 42\n")
        call = ToolCall(id="c1", name="grep", arguments={"pattern": "hello", "path": str(f)})
        result = await registry.execute(call)
        assert "hello" in result.content
        assert "2:" in result.content

    async def test_search_dir(self, tmp_path, registry):
        (tmp_path / "a.py").write_text("target = 1\n")
        (tmp_path / "b.py").write_text("other = 2\n")
        call = ToolCall(
            id="c1", name="grep", arguments={"pattern": "target", "path": str(tmp_path)}
        )
        result = await registry.execute(call)
        assert "a.py" in result.content
        assert "b.py" not in result.content

    async def test_no_matches(self, tmp_path, registry):
        f = tmp_path / "x.txt"
        f.write_text("nothing here")
        call = ToolCall(id="c1", name="grep", arguments={"pattern": "zzz", "path": str(f)})
        result = await registry.execute(call)
        assert "no matches" in result.content

    async def test_invalid_regex(self, tmp_path, registry):
        call = ToolCall(
            id="c1", name="grep", arguments={"pattern": "[invalid", "path": str(tmp_path)}
        )
        result = await registry.execute(call)
        assert result.is_error


class TestGlob:
    async def test_find_files(self, tmp_path, registry):
        (tmp_path / "a.py").touch()
        (tmp_path / "b.py").touch()
        (tmp_path / "c.txt").touch()
        call = ToolCall(id="c1", name="glob", arguments={"pattern": "*.py", "path": str(tmp_path)})
        result = await registry.execute(call)
        assert "a.py" in result.content
        assert "b.py" in result.content
        assert "c.txt" not in result.content

    async def test_no_files(self, tmp_path, registry):
        call = ToolCall(id="c1", name="glob", arguments={"pattern": "*.xyz", "path": str(tmp_path)})
        result = await registry.execute(call)
        assert "no files" in result.content


class TestTodoPlanExit:
    async def test_todo_add_list(self, registry):
        call = ToolCall(id="c1", name="todo", arguments={"action": "add", "content": "buy milk"})
        result = await registry.execute(call)
        assert "added" in result.content

        call2 = ToolCall(id="c2", name="todo", arguments={"action": "list"})
        result2 = await registry.execute(call2)
        assert "buy milk" in result2.content

    async def test_plan_set_show(self, registry):
        call = ToolCall(
            id="c1", name="task_plan", arguments={"action": "set", "steps": "step 1\nstep 2\nstep 3"}
        )
        result = await registry.execute(call)
        assert "3 steps" in result.content

        call2 = ToolCall(id="c2", name="task_plan", arguments={"action": "show"})
        result2 = await registry.execute(call2)
        assert "step 1" in result2.content
        assert "step 3" in result2.content

    async def test_exit(self, registry):
        call = ToolCall(id="c1", name="exit", arguments={})
        result = await registry.execute(call)
        assert "[SESSION_EXIT]" in result.content


class TestRegistryAllBuiltins:
    def test_all_tools_registered(self, registry):
        expected = {
            "read_file",
            "bash",
            "write_file",
            "edit_file",
            "grep",
            "glob",
            "web_fetch",
            "web_search",
            "execute_code",
            "vision_analyze",
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_type",
            "browser_back",
            "browser_scroll",
            "browser_press",
            "browser_console",
            "browser_get_images",
            "browser_vision",
            "context7",
            "session_search",
            "process",
            "todo",
            "task_plan",
            "exit",
            "task",
            "skill_manage",
            "skills_list",
            "question",
            "lsp",
            "mcp_connect",
            "git",
            "file_tree",
        }
        actual = set(registry.names)
        assert actual == expected, f"missing: {expected - actual}, extra: {actual - expected}"

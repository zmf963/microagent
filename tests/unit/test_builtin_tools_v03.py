"""Tests for v0.3 builtin tools: git, file_tree, write_file backup."""

import asyncio
import tempfile
from pathlib import Path
import subprocess

import pytest

from microagent.core.tool import ToolRegistry
from microagent.core.types import ToolCall
from microagent.tools.builtins.git import git
from microagent.tools.builtins.file_tree import file_tree
from microagent.tools.builtins.write_file import write_file


async def _exec(tool, **kwargs):
    """Helper: execute a FunctionTool with keyword args."""
    call = ToolCall(id="test", name=tool.name, arguments=kwargs)
    return await tool.execute(call)


class TestGitTool:
    def test_git_tool_registered(self):
        """git tool is registered with @tool decorator."""
        assert git.name == "git"

    async def test_git_status(self, tmp_path):
        """git status works in a git repo."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, capture_output=True)
        (tmp_path / "test.txt").write_text("hello")

        result = await _exec(git, subcommand="status", repo_path=str(tmp_path))
        assert not result.is_error
        assert "test.txt" in result.content

    async def test_git_unknown_subcommand_rejected(self):
        """Unknown subcommands are rejected."""
        result = await _exec(git, subcommand="push", repo_path=".")
        assert result.is_error

    async def test_git_whitelist_enforced(self):
        """Only whitelisted subcommands are allowed."""
        result = await _exec(git, subcommand="reset", repo_path=".")
        assert result.is_error
        assert "not allowed" in result.content.lower() or "invalid" in result.content.lower()


class TestFileTreeTool:
    def test_file_tree_registered(self):
        """file_tree tool is registered."""
        assert file_tree.name == "file_tree"

    async def test_file_tree_output(self, tmp_path):
        """file_tree produces a tree structure."""
        (tmp_path / "dir1").mkdir()
        (tmp_path / "dir1" / "file1.py").write_text("x")
        (tmp_path / "file2.py").write_text("y")

        result = await _exec(file_tree, path=str(tmp_path), max_depth=3)
        assert not result.is_error
        assert "dir1" in result.content
        assert "file1.py" in result.content or "file2.py" in result.content

    async def test_file_tree_depth_limit(self, tmp_path):
        """file_tree respects max_depth."""
        (tmp_path / "d1").mkdir()
        (tmp_path / "d1" / "d2").mkdir()
        (tmp_path / "d1" / "d2" / "d3").mkdir()
        (tmp_path / "d1" / "d2" / "d3" / "deep.txt").write_text("deep")

        result = await _exec(file_tree, path=str(tmp_path), max_depth=1)
        assert not result.is_error
        assert "deep.txt" not in result.content  # too deep


class TestWriteFileBackup:
    async def test_write_file_backup_creates_copy(self, tmp_path):
        """write_file with backup=True creates a .bak copy of existing file."""
        target = tmp_path / "target.txt"
        target.write_text("original content")

        result = await _exec(write_file, path=str(target), content="new content", backup=True)
        assert not result.is_error

        # Original content backed up
        backup = tmp_path / "target.txt.bak"
        assert backup.exists()
        assert backup.read_text() == "original content"
        # New content written
        assert target.read_text() == "new content"

    async def test_write_file_no_backup_by_default(self, tmp_path):
        """Without backup=True, no .bak file is created."""
        target = tmp_path / "target.txt"
        target.write_text("original")

        result = await _exec(write_file, path=str(target), content="new")
        assert not result.is_error
        assert not (tmp_path / "target.txt.bak").exists()

    async def test_write_file_backup_no_existing_file(self, tmp_path):
        """backup=True on a new file doesn't create a .bak (nothing to back up)."""
        target = tmp_path / "new.txt"

        result = await _exec(write_file, path=str(target), content="content", backup=True)
        assert not result.is_error
        assert not (tmp_path / "new.txt.bak").exists()

"""Tests for glob, git, vision_analyze, read_file, file_tree, write_file tools."""

import pytest
from pathlib import Path


# ============================ glob ============================
class TestGlob:
    @pytest.mark.asyncio
    async def test_finds_files(self, tmp_path):
        from microagent.tools.builtins.glob import glob
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.py").write_text("z")
        r = await glob.fn(pattern="**/*.py", path=str(tmp_path))
        assert not r.is_error
        assert "a.py" in r.content
        assert "c.py" in r.content
        assert "b.txt" not in r.content

    @pytest.mark.asyncio
    async def test_path_not_found(self):
        from microagent.tools.builtins.glob import glob
        r = await glob.fn(pattern="**/*.py", path="/nonexistent-dir-xyz")
        assert r.is_error
        assert "path not found" in r.content

    @pytest.mark.asyncio
    async def test_no_files(self, tmp_path):
        from microagent.tools.builtins.glob import glob
        r = await glob.fn(pattern="**/*.py", path=str(tmp_path))
        assert not r.is_error
        assert "(no files found)" in r.content

    @pytest.mark.asyncio
    async def test_truncation(self, tmp_path):
        from microagent.tools.builtins.glob import glob
        for i in range(10):
            (tmp_path / f"f{i}.txt").write_text("x")
        r = await glob.fn(pattern="*.txt", path=str(tmp_path), max_results=3)
        assert "truncated at 3 results" in r.content
        # only 3 lines of file paths
        body = r.content.split("[truncated")[0].strip().split("\n")
        assert len(body) == 3


# ============================ git ============================
class TestGit:
    @pytest.mark.asyncio
    async def test_disallowed_subcommand(self, tmp_path):
        from microagent.tools.builtins.git import git
        r = await git.fn(subcommand="push", repo_path=str(tmp_path))
        assert r.is_error
        assert "not allowed" in r.content

    @pytest.mark.asyncio
    async def test_not_a_repo(self, tmp_path):
        from microagent.tools.builtins.git import git
        r = await git.fn(subcommand="status", repo_path=str(tmp_path))
        assert r.is_error

    @pytest.mark.asyncio
    async def test_commit_amend_rejected(self, tmp_path):
        """--amend rewrites local history; the whitelist is for
        forward-only operations. Must be rejected before execution."""
        from microagent.tools.builtins.git import git
        r = await git.fn(subcommand="commit", repo_path=str(tmp_path), args="--amend -m 'x'")
        assert r.is_error
        assert "amend" in r.content.lower()

    @pytest.mark.asyncio
    async def test_branch_delete_rejected(self, tmp_path):
        """git branch -D deletes a branch — a write op smuggled through
        an otherwise read-only subcommand."""
        import subprocess
        from microagent.tools.builtins.git import git
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
        (tmp_path / "f.txt").write_text("x")
        await git.fn(subcommand="add", repo_path=str(tmp_path), args=".")
        await git.fn(subcommand="commit", repo_path=str(tmp_path), args="-m init")
        subprocess.run(["git", "-C", str(tmp_path), "branch", "feature"], check=True)

        for flag in ("-D", "-d"):
            r = await git.fn(subcommand="branch", repo_path=str(tmp_path), args=f"{flag} feature")
            assert r.is_error and "not allowed" in r.content.lower(), (flag, r.content)
        # Branch must still exist — nothing was deleted
        r = await git.fn(subcommand="branch", repo_path=str(tmp_path))
        assert "feature" in r.content

    @pytest.mark.asyncio
    async def test_shlex_split_quoted_args(self, tmp_path):
        import subprocess
        from microagent.tools.builtins.git import git
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
        (tmp_path / "f.txt").write_text("x")
        await git.fn(subcommand="add", repo_path=str(tmp_path), args=".")
        r = await git.fn(subcommand="commit", repo_path=str(tmp_path),
                         args="-m 'a multi word message'")
        assert not r.is_error, r.content
        log = await git.fn(subcommand="log", repo_path=str(tmp_path))
        assert "a multi word message" in log.content

    @pytest.mark.asyncio
    async def test_unbalanced_quotes(self, tmp_path):
        from microagent.tools.builtins.git import git
        r = await git.fn(subcommand="log", repo_path=str(tmp_path), args="-m 'unclosed")
        assert r.is_error

    @pytest.mark.asyncio
    async def test_git_not_installed(self, monkeypatch):
        from microagent.tools.builtins import git as git_mod
        # create_subprocess_exec raises FileNotFoundError when git binary missing
        monkeypatch.setattr(
            git_mod.asyncio, "create_subprocess_exec",
            _fake_create_subprocess,
        )
        r = await git_mod.git.fn(subcommand="status", repo_path=".")
        assert r.is_error
        assert "git not found" in r.content


async def _fake_create_subprocess(*args, **kwargs):
    import asyncio
    raise FileNotFoundError("git not found")


# ============================ vision_analyze ============================
class TestVisionAnalyze:
    @pytest.mark.asyncio
    async def test_blank_image_url(self):
        from microagent.tools.builtins.vision_analyze import vision_analyze
        r = await vision_analyze.fn(image_url="   ")
        assert r.is_error
        assert "image_url is required" in r.content

    @pytest.mark.asyncio
    async def test_data_url_passthrough(self):
        from microagent.tools.builtins.vision_analyze import vision_analyze
        r = await vision_analyze.fn(image_url="data:image/png;base64,AAAA")
        assert not r.is_error
        assert "data:image/png;base64,AAAA" in r.content

    @pytest.mark.asyncio
    async def test_http_url_passthrough(self):
        from microagent.tools.builtins.vision_analyze import vision_analyze
        r = await vision_analyze.fn(image_url="https://example.com/img.png")
        assert not r.is_error
        assert "https://example.com/img.png" in r.content

    @pytest.mark.asyncio
    async def test_local_file_not_found(self):
        from microagent.tools.builtins.vision_analyze import vision_analyze
        r = await vision_analyze.fn(image_url="/nonexistent-image.png")
        assert r.is_error
        assert "image not found" in r.content

    @pytest.mark.asyncio
    async def test_local_file_encoded(self, tmp_path):
        from microagent.tools.builtins.vision_analyze import vision_analyze
        p = tmp_path / "pic.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        r = await vision_analyze.fn(image_url=str(p))
        assert not r.is_error
        assert "data:image/png;base64," in r.content


# ============================ read_file ============================
class TestReadFile:
    @pytest.mark.asyncio
    async def test_read_basic(self, tmp_path):
        from microagent.tools.builtins.read_file import read_file
        p = tmp_path / "f.txt"
        p.write_text("line1\nline2\nline3\n")
        r = await read_file.fn(path=str(p))
        assert not r.is_error
        assert "line1" in r.content and "line3" in r.content

    @pytest.mark.asyncio
    async def test_offset_limit(self, tmp_path):
        from microagent.tools.builtins.read_file import read_file
        p = tmp_path / "f.txt"
        p.write_text("\n".join(f"line{i}" for i in range(10)))
        # offset is 1-indexed: offset=3 starts at "line2" (the 3rd line)
        r = await read_file.fn(path=str(p), offset=3, limit=2)
        assert "line2" in r.content and "line3" in r.content
        assert "line0" not in r.content

    @pytest.mark.asyncio
    async def test_not_found(self):
        from microagent.tools.builtins.read_file import read_file
        r = await read_file.fn(path="/nonexistent-xyz.txt")
        assert r.is_error

    @pytest.mark.asyncio
    async def test_binary_detection(self, tmp_path):
        from microagent.tools.builtins.read_file import read_file
        p = tmp_path / "bin.dat"
        p.write_bytes(b"\x00\x01\x02\xff")
        r = await read_file.fn(path=str(p))
        assert r.is_error
        assert "binary" in r.content.lower()

    @pytest.mark.asyncio
    async def test_empty_file(self, tmp_path):
        from microagent.tools.builtins.read_file import read_file
        p = tmp_path / "empty.txt"
        p.write_text("")
        r = await read_file.fn(path=str(p))
        assert not r.is_error
        assert "empty file" in r.content.lower()

    @pytest.mark.asyncio
    async def test_offset_past_end(self, tmp_path):
        from microagent.tools.builtins.read_file import read_file
        p = tmp_path / "f.txt"
        p.write_text("one\ntwo\n")
        r = await read_file.fn(path=str(p), offset=99)
        assert r.is_error


# ============================ file_tree ============================
class TestFileTree:
    @pytest.mark.asyncio
    async def test_basic_tree(self, tmp_path):
        from microagent.tools.builtins.file_tree import file_tree
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")
        (tmp_path / "README.md").write_text("x")
        r = await file_tree.fn(path=str(tmp_path), max_depth=3)
        assert not r.is_error
        assert "src" in r.content
        assert "README.md" in r.content

    @pytest.mark.asyncio
    async def test_path_not_found(self):
        from microagent.tools.builtins.file_tree import file_tree
        r = await file_tree.fn(path="/nonexistent-xyz")
        assert r.is_error

    @pytest.mark.asyncio
    async def test_not_a_directory(self, tmp_path):
        from microagent.tools.builtins.file_tree import file_tree
        p = tmp_path / "file.txt"
        p.write_text("x")
        r = await file_tree.fn(path=str(p))
        assert r.is_error
        assert "not a directory" in r.content

    @pytest.mark.asyncio
    async def test_ignores_venv_and_pycache(self, tmp_path):
        from microagent.tools.builtins.file_tree import file_tree
        (tmp_path / ".venv").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x")
        r = await file_tree.fn(path=str(tmp_path), max_depth=3)
        assert ".venv" not in r.content
        assert "__pycache__" not in r.content
        assert "main.py" in r.content


# ============================ write_file ============================
class TestWriteFile:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path):
        from microagent.tools.builtins.write_file import write_file
        p = tmp_path / "sub" / "out.txt"
        r = await write_file.fn(path=str(p), content="hello content")
        assert not r.is_error
        assert p.read_text() == "hello content"

    @pytest.mark.asyncio
    async def test_write_overwrites(self, tmp_path):
        from microagent.tools.builtins.write_file import write_file
        p = tmp_path / "f.txt"
        p.write_text("old")
        await write_file.fn(path=str(p), content="new")
        assert p.read_text() == "new"

    @pytest.mark.asyncio
    async def test_write_backup(self, tmp_path):
        from microagent.tools.builtins.write_file import write_file
        p = tmp_path / "f.txt"
        p.write_text("original")
        await write_file.fn(path=str(p), content="changed", backup=True)
        bak = tmp_path / "f.txt.bak"
        assert bak.read_text() == "original"
        assert p.read_text() == "changed"

    @pytest.mark.asyncio
    async def test_write_too_large(self, tmp_path):
        from microagent.tools.builtins.write_file import write_file
        big = "x" * 11_000_000  # > 10MB limit
        r = await write_file.fn(path=str(tmp_path / "big.txt"), content=big)
        assert r.is_error
        assert "too large" in r.content

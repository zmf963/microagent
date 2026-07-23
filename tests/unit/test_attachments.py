"""Tests for attachment file path extraction edge cases."""

import pytest
from microagent.session.attachments import _extract_file_paths, _is_readable_file
from microagent.core.types import Message, ToolCall


class TestFilePathExtraction:
    def test_absolute_path(self):
        """Absolute paths are extracted."""
        msgs = (
            Message.assistant("read", tool_calls=(
                ToolCall(id="c1", name="read_file", arguments={"path": "/etc/hosts"}),
            )),
        )
        files = _extract_file_paths(msgs)
        assert "/etc/hosts" in files

    def test_relative_path(self):
        """Relative paths are extracted."""
        msgs = (
            Message.assistant("read", tool_calls=(
                ToolCall(id="c1", name="read_file", arguments={"path": "src/main.py"}),
            )),
        )
        files = _extract_file_paths(msgs)
        assert "src/main.py" in files

    def test_dot_slash_path(self):
        """Paths starting with ./ are extracted."""
        msgs = (
            Message.assistant("read", tool_calls=(
                ToolCall(id="c1", name="read_file", arguments={"path": "./config.yaml"}),
            )),
        )
        files = _extract_file_paths(msgs)
        assert "./config.yaml" in files or "config.yaml" in files

    def test_tilde_path(self):
        """Paths with ~/ are extracted."""
        msgs = (
            Message.assistant("read", tool_calls=(
                ToolCall(id="c1", name="read_file", arguments={"path": "~/.bashrc"}),
            )),
        )
        files = _extract_file_paths(msgs)
        assert any("bashrc" in f for f in files)

    def test_no_extension_ignored(self):
        """Files without extensions are ignored."""
        msgs = (
            Message.assistant("read", tool_calls=(
                ToolCall(id="c1", name="read_file", arguments={"path": "/usr/bin/python3"}),
            )),
        )
        files = _extract_file_paths(msgs)
        assert len(files) == 0, f"extension-less files should be ignored: {files}"

    def test_urls_ignored(self):
        """URLs are not treated as file paths."""
        msgs = (
            Message.assistant("fetch", tool_calls=(
                ToolCall(id="c1", name="web_fetch", arguments={
                    "url": "https://example.com/data.json"
                }),
            )),
        )
        files = _extract_file_paths(msgs)
        assert not files, f"URLs should be ignored: {files}"

    def test_most_recent_ordered(self):
        """Files are ordered by last seen (most recent first)."""
        msgs = (
            Message.assistant("read", tool_calls=(
                ToolCall(id="c1", name="read_file", arguments={"path": "old.py"}),
            )),
            Message.assistant("read", tool_calls=(
                ToolCall(id="c2", name="read_file", arguments={"path": "new.py"}),
            )),
        )
        files = _extract_file_paths(msgs)
        keys = list(files.keys())
        assert keys[0] == "new.py"  # most recent first

    def test_max_three_files(self):
        """Only 3 most recent files are returned."""
        msgs = tuple(
            Message.assistant("read", tool_calls=(
                ToolCall(id=f"c{i}", name="read_file", arguments={"path": f"file{i}.py"}),
            ))
            for i in range(10)
        )
        files = _extract_file_paths(msgs)
        assert len(files) <= 3


class TestIsReadableFile:
    def test_py_readable(self):
        assert _is_readable_file("app.py")

    def test_yaml_readable(self):
        assert _is_readable_file("config.yaml")

    def test_no_extension_not_readable(self):
        assert not _is_readable_file("README")

    def test_makefile_readable(self):
        assert _is_readable_file("Makefile")

    def test_dockerfile_readable(self):
        assert _is_readable_file("Dockerfile")

    def test_directory_not_readable(self):
        assert not _is_readable_file("/usr/bin/")

    def test_url_not_readable(self):
        assert not _is_readable_file("https://example.com/file.json")

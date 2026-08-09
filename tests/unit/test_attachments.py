"""Tests for attachment file path extraction edge cases."""

from microagent.core.types import Message, ToolCall
from microagent.session.attachments import _extract_file_paths, _is_readable_file


class TestFilePathExtraction:
    def test_absolute_path(self):
        """Absolute paths are extracted."""
        msgs = (
            Message.assistant(
                "read",
                tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "/etc/hosts"}),),
            ),
        )
        files = _extract_file_paths(msgs)
        assert "/etc/hosts" in files

    def test_relative_path(self):
        """Relative paths are extracted."""
        msgs = (
            Message.assistant(
                "read",
                tool_calls=(
                    ToolCall(id="c1", name="read_file", arguments={"path": "src/main.py"}),
                ),
            ),
        )
        files = _extract_file_paths(msgs)
        assert "src/main.py" in files

    def test_dot_slash_path(self):
        """Paths starting with ./ are extracted."""
        msgs = (
            Message.assistant(
                "read",
                tool_calls=(
                    ToolCall(id="c1", name="read_file", arguments={"path": "./config.yaml"}),
                ),
            ),
        )
        files = _extract_file_paths(msgs)
        assert "./config.yaml" in files or "config.yaml" in files

    def test_tilde_path(self):
        """Paths with ~/ are extracted."""
        msgs = (
            Message.assistant(
                "read",
                tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "~/.bashrc"}),),
            ),
        )
        files = _extract_file_paths(msgs)
        assert any("bashrc" in f for f in files)

    def test_no_extension_ignored(self):
        """Files without extensions are ignored."""
        msgs = (
            Message.assistant(
                "read",
                tool_calls=(
                    ToolCall(id="c1", name="read_file", arguments={"path": "/usr/bin/python3"}),
                ),
            ),
        )
        files = _extract_file_paths(msgs)
        assert len(files) == 0, f"extension-less files should be ignored: {files}"

    def test_urls_ignored(self):
        """URLs are not treated as file paths."""
        msgs = (
            Message.assistant(
                "fetch",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="web_fetch",
                        arguments={"url": "https://example.com/data.json"},
                    ),
                ),
            ),
        )
        files = _extract_file_paths(msgs)
        assert not files, f"URLs should be ignored: {files}"

    def test_most_recent_ordered(self):
        """Files are ordered by last seen (most recent first)."""
        msgs = (
            Message.assistant(
                "read",
                tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "old.py"}),),
            ),
            Message.assistant(
                "read",
                tool_calls=(ToolCall(id="c2", name="read_file", arguments={"path": "new.py"}),),
            ),
        )
        files = _extract_file_paths(msgs)
        keys = list(files.keys())
        assert keys[0] == "new.py"  # most recent first

    def test_max_three_files(self):
        """Only 3 most recent files are returned."""
        msgs = tuple(
            Message.assistant(
                "read",
                tool_calls=(
                    ToolCall(id=f"c{i}", name="read_file", arguments={"path": f"file{i}.py"}),
                ),
            )
            for i in range(10)
        )
        files = _extract_file_paths(msgs)
        assert len(files) <= 3

    def test_version_numbers_not_paths(self):
        """Pure version/number strings (5.00, 1.2.3, v1.2.3) are not file
        paths — the bare-filename regex matches them, and each one can
        consume a MAX_FILES slot."""
        msgs = (
            Message.user("we upgraded from 5.00 to v1.2.3, see changelog 2.0.1 notes"),
        )
        files = _extract_file_paths(msgs)
        assert "5.00" not in files
        assert "v1.2.3" not in files
        assert "1.2.3" not in files
        assert "2.0.1" not in files

    def test_existing_files_preferred_over_false_positives(self, tmp_path):
        """When slots are scarce, files that actually exist on disk win
        over regex false positives like domains."""
        real = tmp_path / "real_data.txt"
        real.write_text("x")
        msgs = (
            Message.user(f"docs at example.com, also read {real} please"),
        )
        files = _extract_file_paths(msgs)
        assert str(real) in files
        # The existing file must rank ahead of the non-existent domain
        keys = list(files.keys())
        assert keys[0] == str(real)


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


class TestExtractFromToolCalls:
    def test_extracts_paths_from_read_file_tool_call(self, tmp_path):
        from microagent.session.attachments import _extract_file_paths
        from microagent.core.types import Message, ToolCall
        f = tmp_path / "data.txt"
        f.write_text("x")
        msg = Message.assistant(
            text="",
            tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": str(f)}),),
        )
        paths = _extract_file_paths((msg,))
        assert str(f) in paths

    def test_ignores_other_tool_call_args(self, tmp_path):
        from microagent.session.attachments import _extract_file_paths
        from microagent.core.types import Message, ToolCall
        msg = Message.assistant(
            text="",
            tool_calls=(ToolCall(id="c1", name="web_search", arguments={"query": "hello"}),),
        )
        assert _extract_file_paths((msg,)) == {}

    def test_content_fallback(self, tmp_path):
        from microagent.session.attachments import _extract_file_paths
        from microagent.core.types import Message
        f = tmp_path / "notes.txt"
        f.write_text("x")
        msg = Message.user(f"please read {f} for me")
        paths = _extract_file_paths((msg,))
        assert str(f) in paths

    def test_tool_result_content_not_scanned(self, tmp_path):
        """Tool-result content is untrusted (web pages, command output) — a
        poisoned result naming ~/.aws/credentials must not get the file
        read from disk and injected into LLM context."""
        from microagent.session.attachments import recover_file_attachments
        from microagent.core.types import Message, ToolResult
        secret = tmp_path / "credentials.txt"
        secret.write_text("SECRET-KEY-MATERIAL")
        msgs = (
            Message.user("fetch that page"),
            Message.tool_result(
                ToolResult.ok(f"page body mentions {secret} and other stuff"),
                tool_call_id="c1",
            ),
        )
        result = recover_file_attachments(msgs, (Message.user("summary"),))
        assert len(result) == 1
        assert "SECRET-KEY-MATERIAL" not in "".join(m.content for m in result)


class TestRecoverFileAttachments:
    def test_no_messages_returns_compressed(self):
        from microagent.session.attachments import recover_file_attachments
        assert recover_file_attachments((), ("x",)) == ("x",)

    def test_no_files_returns_compressed(self):
        from microagent.session.attachments import recover_file_attachments
        from microagent.core.types import Message
        msgs = (Message.user("nothing useful"),)
        assert recover_file_attachments(msgs, ("compressed",)) == ("compressed",)

    def test_recovers_file_content(self, tmp_path):
        from microagent.session.attachments import recover_file_attachments
        from microagent.core.types import Message, ToolCall
        f = tmp_path / "recover.txt"
        f.write_text("important file content")
        msgs = (
            Message.assistant(
                text="", tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": str(f)}),)
            ),
        )
        result = recover_file_attachments(msgs, (Message.user("summary"),))
        # One attachment message appended
        assert len(result) == 2
        assert "important file content" in result[1].content

    def test_truncates_large_files(self, tmp_path):
        from microagent.session.attachments import (
            recover_file_attachments, MAX_CHARS_PER_FILE,
        )
        from microagent.core.types import Message, ToolCall
        f = tmp_path / "big.txt"
        f.write_text("x" * (MAX_CHARS_PER_FILE + 1000))
        msgs = (
            Message.assistant(
                text="", tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": str(f)}),)
            ),
        )
        result = recover_file_attachments(msgs, (Message.user("summary"),))
        assert "truncated" in result[1].content

    def test_unreadable_file_skipped(self, tmp_path):
        from microagent.session.attachments import recover_file_attachments
        from microagent.core.types import Message, ToolCall
        # A path that parses but can't be read
        msgs = (
            Message.assistant(
                text="", tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "/nonexistent/x.py"}),)
            ),
        )
        result = recover_file_attachments(msgs, (Message.user("summary"),))
        assert len(result) == 1  # no attachment added

    def test_read_is_byte_bounded(self, tmp_path):
        """Huge mentioned files must not be fully loaded into memory before
        truncation — the read itself is capped at MAX_READ_BYTES."""
        import microagent.session.attachments as att
        from microagent.core.types import Message, ToolCall

        f = tmp_path / "huge.txt"
        f.write_text("y" * (att.MAX_READ_BYTES * 4))
        msgs = (
            Message.assistant(
                text="", tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": str(f)}),)
            ),
        )
        result = att.recover_file_attachments(msgs, (Message.user("summary"),))
        assert len(result) == 2
        # Attached content is truncated to MAX_CHARS_PER_FILE, well under
        # the byte cap — and never held the whole 256KB file as a str.
        assert len(result[1].content) < att.MAX_READ_BYTES


class TestIsReadableFileMore:
    def test_system_paths_skipped(self):
        from microagent.session.attachments import _is_readable_file
        assert _is_readable_file("/bin/ls") is False
        assert _is_readable_file("/proc/cpuinfo") is False

    def test_extensionless_absolute_allowed(self):
        from microagent.session.attachments import _is_readable_file
        assert _is_readable_file("/home/user/app/config") is True

    def test_url_rejected(self):
        from microagent.session.attachments import _is_readable_file
        assert _is_readable_file("https://example.com/x") is False

    def test_too_long_rejected(self):
        from microagent.session.attachments import _is_readable_file
        assert _is_readable_file("/x" * 300) is False

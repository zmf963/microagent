"""Tests for ToolOutputStore — global tool output size management.

50KB limit + 2000 line limit + head/tail 500 char preview + disk persistence.
"""

import tempfile
from pathlib import Path

from microagent.tools.output_store import ToolOutputStore


class TestToolOutputStore:
    def test_small_output_passes_through(self):
        """Output under all limits is returned unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ToolOutputStore(base_dir=Path(tmpdir))
            result = store.process("call_1", "small output", "bash")
            assert result.content == "small output"
            assert not result.saved_to_disk

    def test_large_output_saved_to_disk(self):
        """Output > 50KB is saved to disk with preview."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ToolOutputStore(base_dir=Path(tmpdir))
            large_output = "x" * 60_000
            result = store.process("call_1", large_output, "bash")
            assert result.saved_to_disk
            assert result.content != large_output
            assert "full output saved to" in result.content
            assert len(result.content) < 1500  # preview is short (head+tail+note)

    def test_many_lines_truncated(self):
        """Output > 2000 lines is saved to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ToolOutputStore(base_dir=Path(tmpdir))
            many_lines = "\n".join(f"line {i}" for i in range(2500))
            result = store.process("call_1", many_lines, "bash")
            assert result.saved_to_disk

    def test_head_tail_preview(self):
        """Preview contains head and tail of original output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ToolOutputStore(base_dir=Path(tmpdir))
            output = "HEAD_CONTENT_HERE" + "x" * 60_000 + "TAIL_CONTENT_HERE"
            result = store.process("call_1", output, "bash")
            assert "HEAD_CONTENT_HERE" in result.content
            assert "TAIL_CONTENT_HERE" in result.content

    def test_disk_file_created(self):
        """Actual file is created on disk for large outputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store = ToolOutputStore(base_dir=base)
            large_output = "y" * 60_000
            result = store.process("call_1", large_output, "bash")
            assert result.disk_path is not None
            assert Path(result.disk_path).exists()
            assert Path(result.disk_path).read_text() == large_output

    def test_cleanup_old_files(self):
        """Files older than retention period are deleted."""
        import os
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store = ToolOutputStore(base_dir=base, retention_days=7)
            # Create a file and backdate it
            old_file = base / "old_session" / "call_old.txt"
            old_file.parent.mkdir(parents=True)
            old_file.write_text("old data")
            old_time = time.time() - (8 * 86400)  # 8 days old
            os.utime(old_file, (old_time, old_time))

            store.cleanup_expired()

            assert not old_file.exists()

    def test_recent_files_not_cleaned(self):
        """Recent files are not deleted during cleanup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store = ToolOutputStore(base_dir=base, retention_days=7)
            store.process("call_1", "x" * 60_000, "bash")
            store.cleanup_expired()
            # The file should still exist
            files = list(base.rglob("*.txt"))
            assert len(files) >= 1

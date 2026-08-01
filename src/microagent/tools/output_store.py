"""ToolOutputStore — global tool output size management.

50KB hard limit + 2000 line limit + head/tail 500 char preview.
Large outputs are persisted to disk; ToolResult.content gets a preview.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .safe_id import safe_filename_from_id

MAX_OUTPUT_BYTES = 50_000
MAX_OUTPUT_LINES = 2_000
PREVIEW_CHARS = 500  # head + tail each
RETENTION_DAYS = 7


@dataclass(frozen=True, slots=True)
class ProcessedOutput:
    """Result of processing a tool output."""

    content: str
    saved_to_disk: bool
    disk_path: str | None = None


class ToolOutputStore:
    """Manages tool output size with disk persistence for large results."""

    def __init__(
        self,
        base_dir: Path | None = None,
        max_bytes: int = MAX_OUTPUT_BYTES,
        max_lines: int = MAX_OUTPUT_LINES,
        preview_chars: int = PREVIEW_CHARS,
        retention_days: int = RETENTION_DAYS,
    ):
        if base_dir is None:
            base_dir = Path.home() / ".microagent" / "tool_outputs"
        self.base_dir = base_dir
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.preview_chars = preview_chars
        self.retention_days = retention_days

    def process(
        self,
        tool_call_id: str,
        content: str,
        tool_name: str,
        session_id: str = "default",
    ) -> ProcessedOutput:
        """Check if output exceeds limits; if so, save to disk and return preview."""
        if len(content) <= self.max_bytes and content.count("\n") + 1 <= self.max_lines:
            return ProcessedOutput(content=content, saved_to_disk=False)

        # Save to disk. Hash the session/tool_call ids so a hostile or
        # malformed id (e.g. '../../etc/cron.d/x') cannot traverse outside
        # base_dir via the constructed path.
        out_dir = self.base_dir / safe_filename_from_id(session_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / f"{safe_filename_from_id(tool_call_id)}.txt"
        file_path.write_text(content)

        # Build preview: head + tail
        head = content[: self.preview_chars]
        tail = content[-self.preview_chars :] if len(content) > self.preview_chars * 2 else ""
        preview = (
            f"{head}\n"
            f"\n... [{len(content)} chars, {content.count(chr(10)) + 1} lines — "
            f"full output saved to {file_path}]\n\n"
            f"{tail}"
        )
        return ProcessedOutput(
            content=preview,
            saved_to_disk=True,
            disk_path=str(file_path),
        )

    def cleanup_expired(self) -> int:
        """Delete files older than retention_days. Returns count deleted."""
        if not self.base_dir.exists():
            return 0

        cutoff = time.time() - (self.retention_days * 86400)
        deleted = 0

        for file_path in self.base_dir.rglob("*.txt"):
            try:
                stat = file_path.stat()
                if stat.st_mtime < cutoff:
                    file_path.unlink()
                    deleted += 1
            except OSError:
                continue

        # Clean up empty session directories
        for session_dir in self.base_dir.iterdir():
            if session_dir.is_dir() and not any(session_dir.iterdir()):
                try:
                    session_dir.rmdir()
                except OSError:
                    pass

        return deleted

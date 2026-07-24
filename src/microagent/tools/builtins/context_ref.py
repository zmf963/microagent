"""@file: reference parser — extract file references from user messages.

Supports: @file:path, @file:path:line, @file:path:start-end
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from ...core.tool import tool
from ...core.types import ToolResult

_FILE_REF_PATTERN = re.compile(r"@file:(.+?)(?::(\d+)(?:-(\d+))?)?(?=\s|$)")


@dataclass(frozen=True, slots=True)
class FileReference:
    """A parsed @file: reference."""

    path: str
    line_start: int | None = None
    line_end: int | None = None

    async def read(self, context_lines: int = 0) -> str:
        """Read file content, optionally limited to line range."""
        p = Path(self.path).expanduser().resolve()
        if not p.exists():
            return f"[file not found: {self.path}]"

        try:
            content = await asyncio.to_thread(p.read_text)
        except Exception as e:
            return f"[failed to read {self.path}: {e!r}]"

        if self.line_start is None:
            return content

        lines = content.splitlines()
        start = max(0, self.line_start - 1)  # 0-indexed
        end = self.line_end or self.line_start
        end = min(end, len(lines))

        result_lines = []
        for i in range(start, end):
            result_lines.append(f"{i + 1}: {lines[i]}")
        return "\n".join(result_lines) if result_lines else "(empty range)"


def parse_file_ref(text: str) -> FileReference | None:
    """Parse a single @file: reference from text.

    Returns None if text doesn't start with @file:.
    """
    m = _FILE_REF_PATTERN.match(text)
    if not m:
        return None

    path = m.group(1)
    line_start = int(m.group(2)) if m.group(2) else None
    line_end = int(m.group(3)) if m.group(3) else None

    return FileReference(path=path, line_start=line_start, line_end=line_end)


def parse_file_refs(text: str) -> list[FileReference]:
    """Parse all @file: references from text."""
    refs = []
    for m in _FILE_REF_PATTERN.finditer(text):
        path = m.group(1)
        line_start = int(m.group(2)) if m.group(2) else None
        line_end = int(m.group(3)) if m.group(3) else None
        refs.append(FileReference(path=path, line_start=line_start, line_end=line_end))
    return refs

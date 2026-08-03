"""write_file builtin tool — write content to a file, overwriting if exists."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool(
    "write_file", description="Write content to a file. Creates parent dirs. Overwrites if exists."
)
async def write_file(
    path: Annotated[str, Field(description="Path to the file to write")],
    content: Annotated[str, Field(description="The content to write")],
    backup: Annotated[bool, Field(description="If True, create a .bak copy of existing file before overwriting")] = False,
) -> ToolResult:
    MAX_FILE_SIZE = 10_000_000  # 10 MB
    content_bytes_len = len(content.encode("utf-8"))

    if content_bytes_len > MAX_FILE_SIZE:
        return ToolResult.error(
            f"content too large: {content_bytes_len} bytes exceeds {MAX_FILE_SIZE} limit"
        )

    p = Path(path).expanduser().resolve()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)

        # Create backup if requested and file exists.
        # Use read_bytes/write_bytes so binary files don't crash on
        # UnicodeDecodeError (read_text assumes UTF-8).
        overwrote_bak = False
        if backup and p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            overwrote_bak = bak.exists()
            bak.write_bytes(p.read_bytes())

        p.write_text(content)
        msg = f"wrote {content_bytes_len} bytes to {p}"
        if overwrote_bak:
            msg += " (overwrote existing backup)"
        return ToolResult.ok(msg)
    except Exception as e:
        return ToolResult.error(f"failed to write: {e!r}")

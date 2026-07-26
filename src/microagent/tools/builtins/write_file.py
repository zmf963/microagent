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

    if len(content) > MAX_FILE_SIZE:
        return ToolResult.error(
            f"content too large: {len(content)} bytes exceeds {MAX_FILE_SIZE} limit"
        )

    p = Path(path).expanduser().resolve()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)

        # Create backup if requested and file exists
        if backup and p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_text(p.read_text())

        p.write_text(content)
        return ToolResult.ok(f"wrote {len(content)} bytes to {p}")
    except Exception as e:
        return ToolResult.error(f"failed to write: {e!r}")

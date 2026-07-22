"""write_file builtin tool — write content to a file, overwriting if exists."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool("write_file", description="Write content to a file. Creates parent dirs. Overwrites if exists.")
async def write_file(
    path: Annotated[str, Field(description="Path to the file to write")],
    content: Annotated[str, Field(description="The content to write")],
) -> ToolResult:
    import asyncio
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_text, content)
        return ToolResult.ok(f"wrote {len(content)} bytes to {path}")
    except Exception as e:
        return ToolResult.error(f"failed to write: {e!r}")

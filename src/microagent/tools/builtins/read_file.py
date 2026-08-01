"""read_file builtin tool — read file contents by line."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# Hard ceiling on file size — read_file loads the whole file before slicing,
# so a multi-GB log would OOM or block the event loop for seconds.
_MAX_READ_BYTES = 50 * 1024 * 1024  # 50 MB


@tool("read_file", description="Read file contents. Returns lines as text.")
async def read_file(
    path: Annotated[str, Field(description="Path to the file to read")],
    offset: Annotated[int, Field(description="Line number to start from (1-indexed)", ge=1)] = 1,
    limit: Annotated[
        int, Field(description="Maximum number of lines to read", ge=1, le=2000)
    ] = 500,
) -> ToolResult:
    p = Path(path).expanduser().resolve()

    if not p.exists():
        return ToolResult.error(f"file not found: {path}")
    if not p.is_file():
        return ToolResult.error(f"not a file: {path}")

    # Guard against OOM before reading: stat first, reject oversized files.
    try:
        size = p.stat().st_size
    except OSError as e:
        return ToolResult.error(f"cannot stat {path}: {e}")
    if size > _MAX_READ_BYTES:
        return ToolResult.error(
            f"file too large: {size:,} bytes exceeds {_MAX_READ_BYTES:,} byte limit. "
            f"Use grep or offset/limit on a streamed reader instead."
        )

    # Read in a thread so a slow/network filesystem doesn't block the loop.
    # (grep.py already does this; read_file was synchronous.)
    raw = await asyncio.to_thread(p.read_bytes)
    if b"\x00" in raw:
        return ToolResult.error(f"binary file, cannot display: {path}")

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    total = len(lines)
    start = max(0, offset - 1)
    end = min(start + limit, total)
    selected = lines[start:end]

    # Format with line numbers
    numbered = [f"{start + i + 1:6d}|{line}" for i, line in enumerate(selected)]
    result = "\n".join(numbered)

    if end < total:
        result += f"\n[truncated: showing {end - start} of {total} lines]"

    if not result:
        # Distinguish "empty file" from "offset past end of file".
        if total == 0:
            result = "(empty file)"
        else:
            return ToolResult.error(
                f"offset {offset} is past the end of file ({total} lines)"
            )

    return ToolResult.ok(result)

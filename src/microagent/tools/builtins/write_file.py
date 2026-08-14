"""write_file builtin tool — write content to a file, overwriting if exists."""

from __future__ import annotations

import asyncio
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

    def _write() -> ToolResult:
        p = Path(path).expanduser().resolve()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)

            # Create backup if requested and file exists.
            # Use read_bytes/write_bytes so binary files don't crash on
            # UnicodeDecodeError (read_text assumes UTF-8).
            overwrote_bak = False
            if backup and p.exists():
                # Bound the backup read: read_bytes() on a multi-GB
                # existing file OOMs the agent before anything is written.
                # 10 MB mirrors the content cap — a larger file is refused
                # rather than copied.
                size = p.stat().st_size
                if size > MAX_FILE_SIZE:
                    return ToolResult.error(
                        f"backup refused: existing file is {size} bytes "
                        f"(over {MAX_FILE_SIZE} limit)"
                    )
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

    # Disk I/O (mkdir, stat, read, write) runs in a worker thread — a slow
    # or network filesystem must not stall the event loop and every
    # concurrent tool call with it.
    return await asyncio.to_thread(_write)

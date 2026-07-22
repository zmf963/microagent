"""grep builtin tool — search file contents using regex."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool("grep", description="Search file contents by regex. Returns matching lines with line numbers.")
async def grep(
    pattern: Annotated[str, Field(description="Regular expression pattern to search for")],
    path: Annotated[str, Field(description="Directory or file to search in")] = ".",
    glob: Annotated[str, Field(description="File name glob pattern (e.g. '*.py')")] = "**/*",
    max_results: Annotated[int, Field(description="Maximum matches to return", ge=1, le=500)] = 50,
) -> ToolResult:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return ToolResult.error(f"invalid regex: {e}")

    root = Path(path)
    if not root.exists():
        return ToolResult.error(f"path not found: {path}")

    matches: list[str] = []

    if root.is_file():
        files = [root]
    else:
        files = sorted(root.glob(glob))

    for fpath in files:
        if not fpath.is_file():
            continue
        # Skip binary files
        try:
            raw = fpath.read_bytes()
            if b"\x00" in raw:
                continue
            lines = raw.decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for i, line in enumerate(lines, 1):
            if regex.search(line):
                rel = str(fpath)
                matches.append(f"{rel}:{i}: {line.strip()}")
                if len(matches) >= max_results:
                    matches.append(f"[truncated at {max_results} results]")
                    return ToolResult.ok("\n".join(matches))

    if not matches:
        return ToolResult.ok("(no matches)")

    return ToolResult.ok("\n".join(matches))

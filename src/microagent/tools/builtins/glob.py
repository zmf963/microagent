"""glob builtin tool — find files by name pattern."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool("glob", description="Find files by glob pattern. Returns sorted file paths.")
async def glob(
    pattern: Annotated[str, Field(description="Glob pattern (e.g. '**/*.py', 'src/**/*.ts')")],
    path: Annotated[str, Field(description="Base directory to search from")] = ".",
    max_results: Annotated[int, Field(description="Maximum files to return", ge=1, le=1000)] = 100,
) -> ToolResult:
    root = Path(path)
    if not root.exists():
        return ToolResult.error(f"path not found: {path}")

    results = sorted(p for p in root.glob(pattern) if p.is_file())

    if not results:
        return ToolResult.ok("(no files found)")

    truncated = False
    if len(results) > max_results:
        results = results[:max_results]
        truncated = True

    output = "\n".join(str(r) for r in results)
    if truncated:
        output += f"\n[truncated at {max_results} results]"

    return ToolResult.ok(output)

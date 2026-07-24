"""file_tree builtin tool — directory tree visualization."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

_IGNORE_DIRS = frozenset({
    "__pycache__", ".git", ".venv", "venv", "node_modules",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build",
    ".eggs", "*.egg-info",
})


@tool(
    "file_tree",
    description="Display a directory tree structure. Useful for understanding project layout.",
)
async def file_tree(
    path: Annotated[str, Field(description="Root directory path")] = ".",
    max_depth: Annotated[int, Field(description="Maximum depth to traverse", ge=1, le=10)] = 3,
) -> ToolResult:
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return ToolResult.error(f"path not found: {path}")
    if not root.is_dir():
        return ToolResult.error(f"not a directory: {path}")

    lines: list[str] = [root.name + "/"]

    def _walk(dir_path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return

        for i, entry in enumerate(entries):
            # Skip ignored directories
            if entry.is_dir() and entry.name in _IGNORE_DIRS:
                continue
            # Skip egg-info pattern
            if entry.name.endswith(".egg-info"):
                continue

            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "

            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension, depth + 1)
            else:
                lines.append(f"{prefix}{connector}{entry.name}")

    _walk(root, "", 1)

    # Limit output size
    result = "\n".join(lines)
    if len(result) > 10_000:
        result = result[:10_000] + "\n... [truncated]"

    return ToolResult.ok(result)

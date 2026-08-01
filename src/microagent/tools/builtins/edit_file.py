"""edit_file builtin tool — find-and-replace within a file."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool("edit_file", description="Edit a file by replacing old_string with new_string.")
async def edit_file(
    path: Annotated[str, Field(description="Path to the file to edit")],
    old_string: Annotated[str, Field(description="The text to find")],
    new_string: Annotated[str, Field(description="The replacement text")],
    replace_all: Annotated[
        bool, Field(description="Replace all occurrences if true, else only first")
    ] = False,
) -> ToolResult:
    MAX_REPLACEMENT_SIZE = 5_000_000  # 5 MB

    # Empty old_string + replace_all corrupts the file: str.replace("", X)
    # inserts X between every character (count("") = len(text)+1). The
    # non-replace_all path is saved by the count>1 uniqueness check, but
    # replace_all has no such guard.
    if not old_string:
        return ToolResult.error("old_string must be non-empty")

    if len(new_string) > MAX_REPLACEMENT_SIZE:
        return ToolResult.error(
            f"new_string too large: {len(new_string)} bytes exceeds {MAX_REPLACEMENT_SIZE} limit"
        )

    p = Path(path).expanduser().resolve()

    if not p.exists():
        return ToolResult.error(f"file not found: {path}")

    text = await asyncio.to_thread(p.read_text)
    count = text.count(old_string)

    if count == 0:
        return ToolResult.error(f"old_string not found in {path}")

    if replace_all:
        new_text = text.replace(old_string, new_string)
    else:
        if count > 1:
            return ToolResult.error(
                f"old_string matches {count} times in {path}. "
                f"Use replace_all=true to replace all, or make old_string more specific."
            )
        new_text = text.replace(old_string, new_string, 1)

    await asyncio.to_thread(p.write_text, new_text)
    actual = count if replace_all else 1
    return ToolResult.ok(f"replaced {actual} occurrence(s) in {path}")

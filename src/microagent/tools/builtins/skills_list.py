"""skills_list builtin tool — list available skills to the LLM.

Gives the LLM visibility into which skills are available so it can
proactively load them via skill_manage or request them from the user.
"""

from __future__ import annotations

import contextvars
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# ContextVar set by SessionRunner.__init__ so tools can access the loader
_current_loader: contextvars.ContextVar = contextvars.ContextVar(
    "skills_list_current_loader", default=None
)


def _set_loader(loader: object) -> None:
    _current_loader.set(loader)


@tool("skills_list", description="List available skills by name and description.")
async def skills_list(
    query: Annotated[
        str, Field(description="Optional search term to filter skills")
    ] = "",
) -> ToolResult:
    """Return available skill names and descriptions."""
    loader = _current_loader.get()
    if loader is None:
        return ToolResult.ok("(no skills configured)")

    try:
        skills = await loader.load()
    except Exception:
        return ToolResult.ok("(failed to load skills)")

    if not skills:
        return ToolResult.ok("(no skills available)")

    query_lower = query.lower().strip() if query else ""
    lines = []
    for s in skills:
        if query_lower and query_lower not in s.name.lower() and query_lower not in s.description.lower():
            continue
        desc = s.description[:100] if s.description else "(no description)"
        lines.append(f"  [{s.namespace}] {s.name} — {desc}")

    if not lines:
        return ToolResult.ok(f"(no skills matching '{query}')")

    return ToolResult.ok("Available skills:\n" + "\n".join(lines))

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
    except Exception as e:
        # Error, not "(failed to load skills)" as an OK result: the model
        # can't distinguish a broken skills dir from "no skills", and
        # skips skill-based workflows without knowing why.
        return ToolResult.error(f"failed to load skills: {e!r}")

    if not skills:
        return ToolResult.ok("(no skills available)")

    query_lower = query.lower().strip() if query else ""
    lines = []
    for s in skills:
        desc_text = s.description or ""  # user-authored skills may lack a description
        if query_lower and query_lower not in s.name.lower() and query_lower not in desc_text.lower():
            continue
        desc = desc_text[:100] if desc_text else "(no description)"
        lines.append(f"  [{s.namespace}] {s.name} — {desc}")

    if not lines:
        return ToolResult.ok(f"(no skills matching '{query}')")

    return ToolResult.ok("Available skills:\n" + "\n".join(lines))

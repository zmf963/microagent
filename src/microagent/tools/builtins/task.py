"""task builtin tool — spawn a subagent to handle a task.

The subagent runs in an isolated context with filtered tools and
independent budget. Only the final text result is returned to the
parent — intermediate tool calls are invisible (context firewall).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from ...subagent.manager import SubagentManager


# Singleton — built at import time with default subagent specs
_manager = SubagentManager()


@tool("task", description="Spawn a subagent to handle a task. Returns only the final result.")
async def task(
    goal: Annotated[str, Field(description="The task for the subagent to complete")],
    subagent_type: Annotated[str, Field(description="Subagent type: explore | general")] = "general",
    context: Annotated[str, Field(description="Background info for the subagent")] = "",
) -> ToolResult:
    prompt = goal
    if context:
        prompt = f"Context:\n{context}\n\nTask:\n{goal}"

    try:
        # We need access to the parent runner. In M3a, we use a
        # thread-local or context-var approach. For now, task tool
        # is registered but requires the runner to be injected.
        result = await _manager.spawn(
            subagent_type, prompt,
            _current_runner,  # set by SessionRunner before tool execution
        )
        return ToolResult.ok(result)
    except KeyError:
        return ToolResult.error(f"unknown subagent type: {subagent_type}")
    except Exception as e:
        return ToolResult.error(f"subagent failed: {e!r}")


# ---------------------------------------------------------------------------
# Context variable to pass parent runner to task tool
# ---------------------------------------------------------------------------

_current_runner: object = None

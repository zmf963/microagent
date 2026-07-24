"""task builtin tool — spawn a subagent to handle a task.

The subagent runs in an isolated context with filtered tools and
independent budget. Only the final text result is returned to the
parent — intermediate tool calls are invisible (context firewall).
"""

from __future__ import annotations

import contextvars
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from ...subagent.manager import SubagentManager

# Singleton — built at import time with default subagent specs
_manager = SubagentManager()

# ContextVar for passing parent runner to task tool (thread-safe + async-safe)
_current_runner: contextvars.ContextVar = contextvars.ContextVar(
    "task_current_runner", default=None
)


@tool("task", description="Spawn a subagent to handle a task. Returns only the final result.")
async def task(
    goal: Annotated[str, Field(description="The task for the subagent to complete")],
    subagent_type: Annotated[
        str, Field(description="Subagent type: explore | general")
    ] = "general",
    context: Annotated[str, Field(description="Background info for the subagent")] = "",
) -> ToolResult:
    prompt = goal
    if context:
        prompt = f"Context:\n{context}\n\nTask:\n{goal}"

    try:
        runner = _current_runner.get()
        if runner is None:
            return ToolResult.error("task tool: runner not available (not in a session)")
        result = await _manager.spawn(
            subagent_type,
            prompt,
            runner,
        )
        return ToolResult.ok(result)
    except KeyError:
        return ToolResult.error(f"unknown subagent type: {subagent_type}")
    except Exception as e:
        return ToolResult.error(f"subagent failed: {e!r}")

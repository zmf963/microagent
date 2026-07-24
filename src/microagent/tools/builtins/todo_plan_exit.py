"""todo + plan + exit builtin tools — in-memory state tools, no external I/O.

These tools manage lightweight in-process state. The session-level
state (todo list, plan items) is stored in a per-session registry via
ContextVar, providing isolation between concurrent Agent sessions.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# ---------------------------------------------------------------------------
# Per-session in-process state (ContextVar — same pattern as process.py)
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """Per-session in-memory state (todos + plan)."""

    todos: list[dict] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)


_current_state: contextvars.ContextVar[SessionState | None] = contextvars.ContextVar(
    "todo_plan_current_state", default=None
)


def _get_state() -> SessionState:
    """Get the current session's state.

    When running inside a SessionRunner, the ContextVar is set to the
    runner's state. When called directly (e.g., in tests without a
    runner), a temporary state is lazily created and stored.
    """
    state = _current_state.get()
    if state is None:
        state = SessionState()
        _current_state.set(state)
    return state


# ---------------------------------------------------------------------------
# todo
# ---------------------------------------------------------------------------


@tool("todo", description="Manage a TODO list. Actions: list, add, update, remove.")
async def todo(
    action: Annotated[str, Field(description="Action: list | add | update | remove")],
    item_id: Annotated[int, Field(description="Item index (0-based) for update/remove", ge=0)] = 0,
    content: Annotated[str, Field(description="Task description (for add/update)")] = "",
    status: Annotated[
        str,
        Field(
            description="Status: pending | in_progress | completed",
        ),
    ] = "pending",
) -> ToolResult:
    state = _get_state()
    todos = state.todos

    if action == "list":
        if not todos:
            return ToolResult.ok("(no todos)")
        lines = [f"  {i}: [{t['status']}] {t['content']}" for i, t in enumerate(todos)]
        return ToolResult.ok("\n".join(lines))

    elif action == "add":
        if not content:
            return ToolResult.error("content is required for add")
        todos.append({"content": content, "status": status})
        return ToolResult.ok(f"added todo #{len(todos) - 1}: {content}")

    elif action == "update":
        if item_id >= len(todos):
            return ToolResult.error(f"todo #{item_id} not found")
        if content:
            todos[item_id]["content"] = content
        todos[item_id]["status"] = status
        return ToolResult.ok(f"updated todo #{item_id}")

    elif action == "remove":
        if item_id >= len(todos):
            return ToolResult.error(f"todo #{item_id} not found")
        removed = todos.pop(item_id)
        return ToolResult.ok(f"removed: {removed['content']}")

    else:
        return ToolResult.error(f"unknown action: {action}. Use: list, add, update, remove")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@tool("plan", description="Create or view a multi-step plan (does not execute).")
async def plan(
    action: Annotated[str, Field(description="Action: show | set | clear")],
    steps: Annotated[str, Field(description="Newline-separated steps (for set action)")] = "",
) -> ToolResult:
    state = _get_state()

    if action == "show":
        if not state.plan:
            return ToolResult.ok("(no plan set)")
        lines = [f"  {i + 1}. {s}" for i, s in enumerate(state.plan)]
        return ToolResult.ok("\n".join(lines))

    elif action == "set":
        if not steps.strip():
            return ToolResult.error("steps is required for set")
        state.plan = [s.strip() for s in steps.strip().split("\n") if s.strip()]
        return ToolResult.ok(f"plan set with {len(state.plan)} steps")

    elif action == "clear":
        state.plan = []
        return ToolResult.ok("plan cleared")

    else:
        return ToolResult.error(f"unknown action: {action}. Use: show, set, clear")


# ---------------------------------------------------------------------------
# exit
# ---------------------------------------------------------------------------


@tool("exit", description="Signal that the task is complete and the session should end.")
async def exit() -> ToolResult:
    return ToolResult.ok("[SESSION_EXIT]")

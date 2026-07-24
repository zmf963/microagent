"""session_search builtin tool — search past sessions for messages."""

from __future__ import annotations

import contextvars
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from ...session.search import search_sessions
from ...core.store import SQLiteStore

# ContextVar for passing store to session_search tool (thread-safe + async-safe)
_current_store: contextvars.ContextVar = contextvars.ContextVar(
    "session_search_current_store", default=None
)


@tool("session_search", description="Search past conversation history for relevant messages.")
async def session_search(
    query: Annotated[str, Field(description="Search query")],
    k: Annotated[int, Field(description="Max results", ge=1, le=20)] = 5,
) -> ToolResult:
    if not query.strip():
        return ToolResult.error("query is required")

    store = _current_store.get()
    if store is None:
        return ToolResult.error("session store not available")

    try:
        results = await search_sessions(store, query, k=k)
    except Exception as e:
        return ToolResult.error(f"search failed: {e!r}")

    if not results:
        return ToolResult.ok("(no matching messages found)")

    lines = []
    for i, msg in enumerate(results, 1):
        role = msg.role.upper()
        text = msg.content[:200]
        lines.append(f"{i}. [{role}] {text}")
    return ToolResult.ok("\n".join(lines))

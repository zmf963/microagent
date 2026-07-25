"""mcp_connect builtin tool — connect to an MCP server at runtime.

Uses the built-in MCP catalog to resolve server names into concrete
commands, then connects via stdio and registers the server's tools.
Active connections are tracked per-session and cleaned up on close.
"""

from __future__ import annotations

import contextvars
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from ...mcp.catalog import BUILTIN_MCP_SERVERS, get_server

# Per-session MCP manager storage (kept alive to prevent GC)
_current_managers: contextvars.ContextVar[dict[str, object] | None] = (
    contextvars.ContextVar("mcp_connect_managers", default=None)
)


def _get_managers() -> dict[str, object]:
    mgr = _current_managers.get()
    if mgr is None:
        mgr = {}
        _current_managers.set(mgr)
    return mgr


@tool(
    "mcp_connect",
    description="Connect to an MCP server and register its tools. Use a catalog name or raw command.",
)
async def mcp_connect(
    name: Annotated[
        str,
        Field(
            description="MCP server name from catalog (e.g. 'git', 'sqlite', 'fetch') "
            "or 'raw:<command>' (e.g. 'raw:npx -y @modelcontextprotocol/server-github')"
        ),
    ],
) -> ToolResult:
    """Connect to an MCP server and register its tools."""
    import shlex

    if name.startswith("raw:"):
        command = tuple(shlex.split(name[4:]))
    else:
        spec = get_server(name)
        if spec is None:
            available = ", ".join(s.name for s in BUILTIN_MCP_SERVERS)
            return ToolResult.error(
                f"MCP server '{name}' not found in catalog. "
                f"Available: {available}. "
                f"Or use 'raw:<command>' for custom servers."
            )
        command = spec.command

    from ...mcp.client import connect_mcp_stdio

    from . import task as _task_mod

    runner = _task_mod._current_runner.get()
    if runner is None:
        return ToolResult.error(
            "mcp_connect: no active session runner."
        )

    try:
        before_count = len(runner.registry.names)
        manager = await connect_mcp_stdio(command, runner.registry)
        after_count = len(runner.registry.names)

        # Keep manager alive for session lifetime
        mgr_id = name if not name.startswith("raw:") else f"raw_{abs(hash(command))}"
        _get_managers()[mgr_id] = manager

        return ToolResult.ok(
            f"Connected to MCP server '{mgr_id}'. "
            f"Registered {after_count - before_count} new tool(s)."
        )
    except ImportError:
        return ToolResult.error(
            "mcp package not installed. Install with: pip install mcp"
        )
    except Exception as e:
        return ToolResult.error(f"MCP connection failed: {e!r}")

"""mcp_connect builtin tool — connect to an MCP server at runtime.

Uses the built-in MCP catalog to resolve server names into concrete
commands, then connects via stdio and registers the server's tools.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from ...mcp.catalog import BUILTIN_MCP_SERVERS, get_server


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
    """Connect to an MCP server and register its tools.

    Uses the built-in catalog to resolve server names.  Raw commands
    can be passed as 'raw:<space-separated argv>'.
    """
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

    # We need the registry to register tools — accessed via ContextVar
    from ...mcp.client import connect_mcp_stdio

    from . import task as _task_mod

    runner = _task_mod._current_runner.get()
    if runner is None:
        return ToolResult.error(
            "mcp_connect: no active session runner. "
            "MCP connections can only be established during a session."
        )

    try:
        manager = await connect_mcp_stdio(command, runner.registry)
        tool_count = len(runner.registry.names) - len(
            [t for t in runner.registry.to_openai_tools() if t["function"]["name"].startswith("mcp_")]
        )
        return ToolResult.ok(
            f"Connected to MCP server. Registered tools: {[n for n in runner.registry.names]}"
        )
    except ImportError:
        return ToolResult.error(
            "mcp package not installed. Install with: pip install mcp"
        )
    except Exception as e:
        return ToolResult.error(f"MCP connection failed: {e!r}")

"""MCP client — bridge external MCP servers into MicroAgent tools.

Uses the official ``mcp`` Python SDK for transport and protocol handling.
Converts MCP tool schemas into MicroAgent ``Tool`` instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.tool import Tool, ToolRegistry
from ..core.types import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class MCPToolAdapter:
    """Wraps an MCP tool call into a MicroAgent Tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    _session: Any  # mcp.ClientSession
    _tool_name: str  # original MCP tool name

    async def execute(
        self, call: ToolCall, ctx: Any = None
    ) -> ToolResult:
        try:
            result = await self._session.call_tool(
                self._tool_name, call.arguments
            )
            content = str(result.content) if result.content else "(empty)"
            return ToolResult.ok(content)
        except Exception as e:
            return ToolResult.error(f"MCP tool {self._tool_name} failed: {e!r}")


async def connect_mcp_stdio(
    command: tuple[str, ...],
    registry: ToolRegistry,
) -> None:
    """Connect to an MCP server via stdio and register its tools.

    Requires ``pip install mcp``.

    Example::

        await connect_mcp_stdio(("uvx", "mcp-server-git"), registry)
    """
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        raise ImportError(
            "mcp package required. Install with: pip install mcp"
        )

    params = StdioServerParameters(command=list(command))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()

            for mcp_tool in tools_result.tools:
                adapter = MCPToolAdapter(
                    name=mcp_tool.name,
                    description=mcp_tool.description or "",
                    parameters=mcp_tool.inputSchema or {"type": "object"},
                    _session=session,
                    _tool_name=mcp_tool.name,
                )
                registry.register(adapter)

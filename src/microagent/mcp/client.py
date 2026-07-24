"""MCP client — bridge external MCP servers into MicroAgent tools.

Uses the official ``mcp`` Python SDK for transport and protocol handling.
Converts MCP tool schemas into MicroAgent ``Tool`` instances.

The connection manager keeps transport and session alive for the lifetime
of the adapter tools. Previously, ``async with`` blocks closed the session
immediately, making all registered MCP tools unusable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..core.tool import Tool, ToolRegistry
from ..core.types import ToolCall, ToolResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MCPToolAdapter:
    """Wraps an MCP tool call into a MicroAgent Tool.

    Uses a module-level session manager — the connection stays alive
    for the lifetime of the adapter.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    _manager: Any  # _MCPConnectionManager — held by reference

    async def execute(
        self, call: ToolCall, ctx: Any = None
    ) -> ToolResult:
        try:
            session = self._manager._session
            if session is None:
                return ToolResult.error(
                    f"MCP tool {self.name}: session not connected"
                )
            result = await session.call_tool(self.name, call.arguments)
            content = str(result.content) if result.content else "(empty)"
            return ToolResult.ok(content)
        except Exception as e:
            return ToolResult.error(f"MCP tool {self.name} failed: {e!r}")


class _MCPConnectionManager:
    """Manages a single MCP server connection (transport + session).

    Starts the transport in a background task, keeps it alive until
    explicitly disconnected. All adapters created from this manager
    share the same session.
    """

    def __init__(self, command: tuple[str, ...]):
        self._command = list(command)
        self._session: Any = None
        self._transport: Any = None
        self._task: asyncio.Task | None = None
        self._tools: list[dict] = []

    async def connect(self) -> None:
        """Start the MCP connection in a background task."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            raise ImportError(
                "mcp package required. Install with: pip install mcp"
            )

        async def _run_connection():
            params = StdioServerParameters(command=self._command)
            async with stdio_client(params) as (read, write):
                self._transport = (read, write)
                async with ClientSession(read, write) as session:
                    self._session = session
                    await session.initialize()
                    tools_result = await session.list_tools()
                    self._tools = [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            "inputSchema": t.inputSchema or {"type": "object"},
                        }
                        for t in tools_result.tools
                    ]
                    # Keep session alive until disconnected
                    try:
                        while True:
                            await asyncio.sleep(3600)  # heartbeat
                    except asyncio.CancelledError:
                        pass

        self._task = asyncio.create_task(_run_connection())

        # Wait for initial connection + tool listing
        for _ in range(50):  # 5 seconds max
            if self._tools:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("MCP server connection timed out")

    async def disconnect(self) -> None:
        """Shut down the MCP connection."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._session = None
        self._transport = None
        self._tools = []

    def register_tools(self, registry: ToolRegistry) -> None:
        """Register all discovered MCP tools into the given registry."""
        for t in self._tools:
            adapter = MCPToolAdapter(
                name=t["name"],
                description=t["description"],
                parameters=t["inputSchema"],
                _manager=self,
            )
            registry.register(adapter)


async def connect_mcp_stdio(
    command: tuple[str, ...],
    registry: ToolRegistry,
) -> _MCPConnectionManager:
    """Connect to an MCP server via stdio and register its tools.

    Returns a connection manager — keep a reference to it to keep
    the connection alive. The transport stays open until
    ``manager.disconnect()`` is called.

    Requires ``pip install mcp``.

    Example::

        manager = await connect_mcp_stdio(("uvx", "mcp-server-git"), registry)
        # ... use tools ...
        await manager.disconnect()
    """
    manager = _MCPConnectionManager(command)
    await manager.connect()
    manager.register_tools(registry)
    return manager

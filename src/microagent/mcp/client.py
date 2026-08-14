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

from ..core.tool import ToolRegistry
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

    async def execute(self, call: ToolCall, ctx: Any = None) -> ToolResult:
        try:
            session = self._manager._session
            if session is None:
                return ToolResult.error(f"MCP tool {self.name}: session not connected")
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
        # Store the executable and args separately — the MCP SDK's
        # StdioServerParameters expects command: str (executable) and
        # args: list[str] (arguments), NOT the full list as command.
        self._command = list(command)
        self._session: Any = None
        self._transport: Any = None
        self._task: asyncio.Task | None = None
        self._tools: list[dict] = []
        self._connected: bool = False  # track connect state independent of tool count
        self._registered_tool_names: list[str] = []  # for unregister on disconnect

    async def connect(self) -> None:
        """Start the MCP connection in a background task."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            raise ImportError("mcp package required. Install with: pip install mcp")

        async def _run_connection():
            # Split command into executable + args for StdioServerParameters.
            # The SDK model has command: str + args: list[str]; passing the
            # full list as command causes a Pydantic ValidationError or, if
            # coerced, loses the args entirely.
            cmd_args = self._command[1:] if len(self._command) > 1 else []
            params = StdioServerParameters(
                command=self._command[0], args=cmd_args,
            )
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
                    # Mark connected only AFTER list_tools completes — the
                    # connect() polling loop returns as soon as this flag is
                    # set; setting it earlier races register_tools() into
                    # permanently registering 0 tools on slow servers.
                    self._connected = True
                    # Keep session alive until disconnected
                    try:
                        while True:
                            await asyncio.sleep(3600)  # heartbeat
                    except asyncio.CancelledError:
                        pass

        self._task = asyncio.create_task(_run_connection())

        # Wait for the connection to be established (NOT for tools to appear).
        # A server that legitimately exposes 0 tools would otherwise trip the
        # old `if self._tools:` check (falsy empty list → spurious timeout).
        for _ in range(50):  # 5 seconds max
            if self._connected:
                break
            if self._task.done():
                # The connection task already ended (server failed to start,
                # e.g. FileNotFoundError: npx missing). Surface the REAL
                # error immediately instead of polling the full 5s and
                # masking it behind a generic "timed out".
                await self._task  # re-raises the task's exception
                raise RuntimeError("MCP server exited before connecting")
            await asyncio.sleep(0.1)
        else:
            # Timed out: cancel the background connection task. Without this
            # the task (and its npx/uvx subprocess) lives forever if the
            # server connects later — and the manager is never tracked
            # anywhere, so close() cannot clean it up.
            await self.disconnect()
            raise RuntimeError("MCP server connection timed out")

    async def disconnect(self, registry: ToolRegistry | None = None) -> None:
        """Shut down the MCP connection.

        ``registry``: when given, the adapter tools this manager
        registered are unregistered first. mcp_connect's dead-server
        reconnect path MUST pass it — the stale adapters would otherwise
        stay registered, and register_tools() on the fresh connection
        raises "duplicate tool" on the first name, making reconnect
        permanently fail.
        """
        if registry is not None and self._registered_tool_names:
            for name in self._registered_tool_names:
                try:
                    registry.unregister(name)
                except Exception:
                    pass
            self._registered_tool_names = []
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
        """Register all discovered MCP tools into the given registry.

        Atomic: if any tool conflicts (e.g. an MCP server exposing
        ``read_file``), already-registered adapters are rolled back so the
        registry is never left half-populated.
        """
        registered: list[str] = []
        try:
            for t in self._tools:
                adapter = MCPToolAdapter(
                    name=t["name"],
                    description=t["description"],
                    parameters=t["inputSchema"],
                    _manager=self,
                )
                registry.register(adapter)
                registered.append(t["name"])
        except ValueError:
            for name in registered:
                registry.unregister(name)
            raise
        else:
            self._registered_tool_names = registered


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
    try:
        manager.register_tools(registry)
    except ValueError as e:
        # Tool-name conflict (common: catalog servers exposing read_file etc).
        # register_tools already rolled back the partial registrations; shut
        # the connection down so the server subprocess is not orphaned.
        await manager.disconnect()
        raise RuntimeError(f"MCP tool registration failed: {e}") from e
    return manager

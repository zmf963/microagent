"""mcp_connect builtin tool — connect to an MCP server at runtime.

Uses the built-in MCP catalog to resolve server names into concrete
commands, then connects via stdio and registers the server's tools.
Active connections are tracked per-session and cleaned up on close.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from ...mcp.catalog import BUILTIN_MCP_SERVERS, get_server
from .._session_state import session_state

# Per-session MCP manager storage (kept alive to prevent GC)
_current_managers, _get_managers = session_state("mcp_connect_managers", dict)
# Per-session lock serializing the idempotency check + connect, so two
# concurrent mcp_connect("git") calls in the same turn's TaskGroup cannot
# both pass the check and leak one orphaned subprocess.
_current_lock, _get_lock = session_state("mcp_connect_lock", asyncio.Lock)


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

    # Stable id for dedup. Hash the joined command string (was hashing the
    # tuple directly → TypeError on every raw: connect, swallowed as
    # "MCP connection failed"). raw: connections have never worked until now.
    import hashlib
    if name.startswith("raw:"):
        mgr_id = "raw_" + hashlib.sha256(" ".join(command).encode()).hexdigest()[:16]
    else:
        mgr_id = name

    managers = _get_managers()
    # Idempotency: if already connected to this server, don't spawn a
    # second subprocess — the first would be orphaned (its _task keeps
    # running but is no longer tracked, leaking the npx/uvx process).
    # The check + connect must be atomic under a per-session lock: two
    # concurrent mcp_connect("git") calls in one turn's TaskGroup would
    # both pass the check and both spawn. Also, a previously-recorded
    # manager whose server process has since died must not pin the slot
    # forever ("already connected" with a dead connection) — drop it and
    # reconnect.
    async with _get_lock():
        existing = managers.get(mgr_id)
        if existing is not None:
            # A dead connection (server crashed) must not pin the slot
            # forever, returning "already connected" against a corpse.
            # Only reconnect when we can positively see the task is done;
            # a manager without a _task attribute (or _task still running)
            # is treated as live (idempotent skip).
            task = getattr(existing, "_task", None)
            if task is None or not task.done():
                return ToolResult.ok(
                    f"MCP server '{mgr_id}' already connected (idempotent)."
                )
            # task is done — clean up the stale entry and reconnect
            try:
                await existing.disconnect()
            except Exception:
                pass
            managers.pop(mgr_id, None)

        try:
            before_count = len(runner.registry.names)
            manager = await connect_mcp_stdio(command, runner.registry)
            after_count = len(runner.registry.names)

            # Keep manager alive for session lifetime
            managers[mgr_id] = manager

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

"""process builtin tool — background process management.

Supports: start, poll, log, kill, wait, list, write.
Processes are tracked in a per-session registry via ContextVar,
providing isolation between concurrent Agent sessions.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# ---------------------------------------------------------------------------
# Per-session process registry (ContextVar — same pattern as _current_store)
# ---------------------------------------------------------------------------


@dataclass
class ProcRegistry:
    """Per-session process and output tracking."""

    procs: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)
    outputs: dict[str, list[str]] = field(default_factory=dict)


_current_registry: contextvars.ContextVar[ProcRegistry | None] = contextvars.ContextVar(
    "process_current_registry", default=None
)


def _get_registry() -> ProcRegistry:
    """Get the current session's process registry.

    When running inside a SessionRunner, the ContextVar is set to the
    runner's registry. When called directly (e.g., in tests without a
    runner), a temporary registry is lazily created and stored.
    """
    reg = _current_registry.get()
    if reg is None:
        reg = ProcRegistry()
        _current_registry.set(reg)
    return reg


def _generate_id() -> str:
    reg = _get_registry()
    return f"proc-{int(time.time() * 1000)}-{len(reg.procs)}"


def _cleanup_dead() -> None:
    """Remove exited processes from registry (called on each action)."""
    reg = _get_registry()
    dead = [sid for sid, p in reg.procs.items() if p.returncode is not None]
    for sid in dead:
        reg.procs.pop(sid, None)
        reg.outputs.pop(sid, None)


@tool("process", description="Manage background processes: start, poll, kill, list, wait, write.")
async def process(
    action: Annotated[str, Field(description="One of: start, poll, kill, list, wait, write")],
    command: Annotated[
        str | None, Field(description="Shell command to run (for action=start)")
    ] = None,
    session_id: Annotated[str | None, Field(description="Process ID from start")] = None,
    data: Annotated[
        str | None, Field(description="Data to write to stdin (for action=write)")
    ] = None,
    timeout: Annotated[float, Field(description="Max seconds for wait action")] = 30,
) -> ToolResult:
    reg = _get_registry()
    match action:
        case "start":
            _cleanup_dead()  # prevent unbounded growth
            if not command:
                return ToolResult.error("command is required for action=start")
            try:
                # start_new_session=True creates a new process group so
                # kill() can signal the whole group (not just /bin/sh),
                # preventing orphaned grandchildren.
                p = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                sid = _generate_id()
                reg.procs[sid] = p
                reg.outputs[sid] = []
                return ToolResult.ok(sid)

            except Exception as e:
                return ToolResult.error(f"start failed: {e!r}")

        case "poll":
            if not session_id or session_id not in reg.procs:
                return ToolResult.error(f"process not found: {session_id}")
            p = reg.procs[session_id]
            if p.returncode is not None:
                # Already done — collect remaining output. Wrap in a timeout
                # because a grandchild holding the stdout pipe open (daemon
                # that inherited the fd) keeps read() blocked forever even
                # after the shell exits.
                out_lines = list(reg.outputs.get(session_id, []))
                if p.stdout:
                    try:
                        remaining = await asyncio.wait_for(p.stdout.read(), timeout=2.0)
                        if remaining:
                            out_lines.append(remaining.decode("utf-8", errors="replace").rstrip())
                    except TimeoutError:
                        out_lines.append("(stdout pipe still open, partial output shown)")
                return ToolResult.ok(f"(exited {p.returncode})\n" + "\n".join(out_lines))
            # Still running — read available output
            try:
                if p.stdout:
                    while True:
                        line = await asyncio.wait_for(p.stdout.readline(), timeout=0.1)
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace").rstrip()
                        reg.outputs[session_id].append(decoded)
            except TimeoutError:
                pass
            return ToolResult.ok("(running)\n" + "\n".join(reg.outputs.get(session_id, [])[-20:]))

        case "log":
            if not session_id or session_id not in reg.outputs:
                return ToolResult.error(f"no output for: {session_id}")
            return ToolResult.ok("\n".join(reg.outputs[session_id]))

        case "kill":
            if not session_id or session_id not in reg.procs:
                return ToolResult.error(f"process not found: {session_id}")
            p = reg.procs[session_id]
            try:
                # Kill the whole process group so grandchildren (the actual
                # workload, not just /bin/sh) are terminated too. Requires
                # start_new_session=True at spawn (which start uses).
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    p.kill()  # fallback: kill just the shell
                await p.wait()
                return ToolResult.ok(f"killed (exit {p.returncode})")
            except Exception as e:
                return ToolResult.error(f"kill failed: {e!r}")

        case "wait":
            if not session_id or session_id not in reg.procs:
                return ToolResult.error(f"process not found: {session_id}")
            p = reg.procs[session_id]
            try:
                await asyncio.wait_for(p.wait(), timeout=timeout)
                stdout, stderr = await p.communicate()
                out = stdout.decode("utf-8", errors="replace")
                if stderr:
                    out += "\n[stderr]\n" + stderr.decode("utf-8", errors="replace")
                return ToolResult.ok(f"(exit {p.returncode})\n{out}")
            except TimeoutError:
                return ToolResult.error(f"timed out after {timeout}s (still running)")

        case "list":
            lines = []
            for sid, p in list(reg.procs.items()):
                status = f"exit={p.returncode}" if p.returncode is not None else "running"
                lines.append(f"{sid}: {status}")
            return ToolResult.ok("\n".join(lines) if lines else "(no processes)")

        case "write":
            if not session_id or session_id not in reg.procs:
                return ToolResult.error(f"process not found: {session_id}")
            if not data:
                return ToolResult.error("data is required for action=write")
            p = reg.procs[session_id]
            if p.stdin is None:
                return ToolResult.error("process has no stdin")
            try:
                p.stdin.write((data + "\n").encode())
                await p.stdin.drain()
                return ToolResult.ok("written")
            except Exception as e:
                return ToolResult.error(f"write failed: {e!r}")

        case _:
            return ToolResult.error(
                f"unknown action: {action}. Valid: start, poll, log, kill, wait, list, write"
            )

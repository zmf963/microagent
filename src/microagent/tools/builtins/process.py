"""process builtin tool — background process management.

Supports: start, poll, log, kill, wait, list, write.
Processes are tracked per-session via ContextVar, providing isolation
between concurrent Agent sessions.

Since v1.2.0 the tool dispatches through a ProcessBackend (terminal/
processes.py) bound by the runner from the session's TerminalBackend —
swapping the terminal backend migrates the whole capability family
(the bash seam from v1.1.1, applied to processes). Without a bound
backend it uses the LocalProcessBackend over this module's per-session
registry, which is behavior-identical to the pre-seam implementation.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from ...terminal.processes import LocalProcessBackend, ProcessBackend
from .._session_state import session_state

# ---------------------------------------------------------------------------
# Per-session process registry (ContextVar — same pattern as _current_store)
# ---------------------------------------------------------------------------


# Output bounds — a spammy process (yes, tail -f, build logs) previously
# (a) hung poll forever: every readline succeeded within the idle timeout
#     so the drain loop never broke, and
# (b) grew the per-process buffer without limit, OOMing the host in seconds.
_MAX_POLL_LINES = 200  # max lines drained per poll call
_MAX_BUFFERED_LINES = 2000  # ring cap per process; older lines are dropped
_MAX_LINE_CHARS = 2000  # per-line truncation


@dataclass
class ProcRegistry:
    """Per-session process and output tracking."""

    procs: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)
    outputs: dict[str, list[str]] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)  # ring-dropped line counts
    # v1.2.0 seam: the canonical ManagedProcess handles (the ring and
    # drain cursors live on the handle — get() must return the SAME
    # instance spawn() created, not a fresh one over the raw Process).
    handles: dict[str, object] = field(default_factory=dict)


def _append_output(reg: ProcRegistry, sid: str, lines: list[str]) -> None:
    """Append lines to the ring buffer, trimming overlong lines and
    dropping the oldest entries beyond _MAX_BUFFERED_LINES."""
    buf = reg.outputs.setdefault(sid, [])
    for line in lines:
        if len(line) > _MAX_LINE_CHARS:
            line = line[:_MAX_LINE_CHARS] + "…[line truncated]"
        buf.append(line)
    overflow = len(buf) - _MAX_BUFFERED_LINES
    if overflow > 0:
        del buf[:overflow]
        reg.dropped[sid] = reg.dropped.get(sid, 0) + overflow


_current_registry, _get_registry = session_state(
    "process_current_registry", ProcRegistry,
)

# Backend seam: the runner binds the session's terminal-backend process
# family here (same pattern as bash's set_backend). None → local default.
_current_backend: contextvars.ContextVar = contextvars.ContextVar(
    "process_current_backend", default=None
)


def set_backend(backend: ProcessBackend | None) -> None:
    """Bind the process backend for the current session context. The
    runner rebinds on every tool execution — direct calls are
    overwritten by the runner's own terminal_backend on the next
    process call in a turn."""
    _current_backend.set(backend)


def _resolve_backend() -> ProcessBackend:
    backend = _current_backend.get()
    if backend is not None:
        return backend
    return LocalProcessBackend(_get_registry())


@tool("process", description="Manage background processes: start, poll, kill, list, wait, write, log.")
async def process(
    action: Annotated[str, Field(description="One of: start, poll, kill, list, wait, write, log")],
    command: Annotated[
        str | None, Field(description="Shell command to run (for action=start)")
    ] = None,
    session_id: Annotated[str | None, Field(description="Process ID from start")] = None,
    data: Annotated[
        str | None, Field(description="Data to write to stdin (for action=write)")
    ] = None,
    timeout: Annotated[float, Field(description="Max seconds for wait action")] = 30,
) -> ToolResult:
    backend = _resolve_backend()
    match action:
        case "start":
            backend.cleanup_dead()  # prevent unbounded growth
            if not command:
                return ToolResult.error("command is required for action=start")
            try:
                handle = await backend.spawn(command)
                return ToolResult.ok(handle.id)
            except Exception as e:
                return ToolResult.error(f"start failed: {e!r}")

        case "poll":
            handle = backend.get(session_id) if session_id else None
            if handle is None:
                return ToolResult.error(f"process not found: {session_id}")
            code = await handle.status()
            if code is not None:
                # Local parity: after exit, collect what's left in the
                # pipe (bounded) before reporting.
                final_drain = getattr(handle, "final_drain", None)
                if final_drain is not None:
                    await final_drain()
                else:
                    await handle.drain()
                return ToolResult.ok(f"(exited {code})\n" + handle.ring.snapshot())
            drained = await handle.drain()
            tail = handle.ring.tail(20)
            if len(drained) >= _MAX_POLL_LINES:
                tail += "\n(more output pending — poll again)"
            return ToolResult.ok("(running)\n" + tail)

        case "log":
            handle = backend.get(session_id) if session_id else None
            if handle is None:
                return ToolResult.error(f"no output for: {session_id}")
            return ToolResult.ok(handle.ring.snapshot())

        case "kill":
            handle = backend.get(session_id) if session_id else None
            if handle is None:
                return ToolResult.error(f"process not found: {session_id}")
            message = await handle.kill()
            if message.startswith("kill failed"):
                return ToolResult.error(message)
            return ToolResult.ok(message)

        case "wait":
            handle = backend.get(session_id) if session_id else None
            if handle is None:
                return ToolResult.error(f"process not found: {session_id}")
            try:
                code, output = await handle.wait(timeout)
            except TimeoutError:
                return ToolResult.error(f"timed out after {timeout}s (still running)")
            if code is None:
                return ToolResult.error(f"timed out after {timeout}s (still running)")
            if output is None:
                output = handle.ring.snapshot()
            return ToolResult.ok(f"(exit {code})\n{output}")

        case "list":
            lines = []
            for h in backend.list():
                status = "running"
                code = await h.status()
                if code is not None:
                    status = f"exit={code}"
                lines.append(f"{h.id}: {status}")
            return ToolResult.ok("\n".join(lines) if lines else "(no processes)")

        case "write":
            handle = backend.get(session_id) if session_id else None
            if handle is None:
                return ToolResult.error(f"process not found: {session_id}")
            if not data:
                return ToolResult.error("data is required for action=write")
            error = await handle.send(data)
            if error is not None:
                return ToolResult.error(error)
            return ToolResult.ok("written")

        case _:
            return ToolResult.error(
                f"unknown action: {action}. Valid: start, poll, log, kill, wait, list, write"
            )

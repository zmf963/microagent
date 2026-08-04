"""process builtin tool — background process management.

Supports: start, poll, log, kill, wait, list, write.
Processes are tracked in a per-session registry via ContextVar,
providing isolation between concurrent Agent sessions.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
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
        reg.dropped.pop(sid, None)


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
                # stdin=PIPE is required so the `write` action can send
                # input to the process (without it, p.stdin is None and
                # write always errors "process has no stdin").
                p = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                sid = _generate_id()
                reg.procs[sid] = p
                reg.outputs[sid] = []
                reg.dropped[sid] = 0
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
                if p.stdout:
                    try:
                        remaining = await asyncio.wait_for(p.stdout.read(), timeout=2.0)
                        if remaining:
                            tail = remaining[-_MAX_LINE_CHARS * 50:]
                            _append_output(
                                reg, session_id,
                                [tail.decode("utf-8", errors="replace").rstrip()],
                            )
                    except TimeoutError:
                        _append_output(reg, session_id, ["(stdout pipe still open, partial output shown)"])
                return ToolResult.ok(f"(exited {p.returncode})\n" + "\n".join(reg.outputs.get(session_id, [])))
            # Still running — drain available output, but bounded: a
            # continuously-outputting process (yes, tail -f) always has a
            # line ready within the idle timeout, so an unbounded loop
            # never returns and hangs the whole agent turn.
            drained = 0
            if p.stdout:
                for _ in range(_MAX_POLL_LINES):
                    try:
                        line = await asyncio.wait_for(p.stdout.readline(), timeout=0.1)
                    except TimeoutError:
                        break  # idle — no more output right now
                    if not line:
                        break  # EOF
                    _append_output(reg, session_id, [line.decode("utf-8", errors="replace").rstrip()])
                    drained += 1
            tail = "\n".join(reg.outputs.get(session_id, [])[-20:])
            if drained >= _MAX_POLL_LINES:
                tail += "\n(more output pending — poll again)"
            return ToolResult.ok("(running)\n" + tail)

        case "log":
            if not session_id or session_id not in reg.outputs:
                return ToolResult.error(f"no output for: {session_id}")
            dropped = reg.dropped.get(session_id, 0)
            body = "\n".join(reg.outputs[session_id])
            if dropped:
                body = f"[{dropped} earlier line(s) dropped — ring buffer cap {_MAX_BUFFERED_LINES}]\n" + body
            return ToolResult.ok(body)

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
                # p.wait() can hang when the pipe holds lots of unread
                # output (asyncio waits for the pipe transport to drain
                # even after the process dies) — bound it.
                try:
                    await asyncio.wait_for(p.wait(), timeout=5.0)
                except TimeoutError:
                    return ToolResult.ok("killed (process-group SIGKILL sent; wait timed out)")
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

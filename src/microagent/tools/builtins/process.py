"""process builtin tool — background process management.

Supports: start, poll, log, kill, wait, list, write.
Processes are tracked in a module-level dict with asyncio subprocess handles.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# Module-level process registry
_procs: dict[str, asyncio.subprocess.Process] = {}
_outputs: dict[str, list[str]] = {}


def _generate_id() -> str:
    return f"proc-{int(time.time() * 1000)}-{len(_procs)}"


def _cleanup_dead() -> None:
    """Remove exited processes from registry (called on each action)."""
    dead = [sid for sid, p in _procs.items() if p.returncode is not None]
    for sid in dead:
        _procs.pop(sid, None)
        _outputs.pop(sid, None)


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
    match action:
        case "start":
            _cleanup_dead()  # prevent unbounded growth
            if not command:
                return ToolResult.error("command is required for action=start")
            try:
                p = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                sid = _generate_id()
                _procs[sid] = p
                _outputs[sid] = []
                return ToolResult.ok(sid)

            except Exception as e:
                return ToolResult.error(f"start failed: {e!r}")

        case "poll":
            if not session_id or session_id not in _procs:
                return ToolResult.error(f"process not found: {session_id}")
            p = _procs[session_id]
            if p.returncode is not None:
                # Already done — collect remaining output
                out_lines = list(_outputs.get(session_id, []))
                if p.stdout:
                    remaining = await p.stdout.read()
                    if remaining:
                        out_lines.append(remaining.decode("utf-8", errors="replace").rstrip())
                return ToolResult.ok(f"(exited {p.returncode})\n" + "\n".join(out_lines))
            # Still running — read available output
            try:
                if p.stdout:
                    while True:
                        line = await asyncio.wait_for(p.stdout.readline(), timeout=0.1)
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace").rstrip()
                        _outputs[session_id].append(decoded)
            except TimeoutError:
                pass
            return ToolResult.ok("(running)\n" + "\n".join(_outputs.get(session_id, [])[-20:]))

        case "log":
            if not session_id or session_id not in _outputs:
                return ToolResult.error(f"no output for: {session_id}")
            return ToolResult.ok("\n".join(_outputs[session_id]))

        case "kill":
            if not session_id or session_id not in _procs:
                return ToolResult.error(f"process not found: {session_id}")
            p = _procs[session_id]
            try:
                p.kill()
                await p.wait()
                return ToolResult.ok(f"killed (exit {p.returncode})")
            except Exception as e:
                return ToolResult.error(f"kill failed: {e!r}")

        case "wait":
            if not session_id or session_id not in _procs:
                return ToolResult.error(f"process not found: {session_id}")
            p = _procs[session_id]
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
            for sid, p in list(_procs.items()):
                status = f"exit={p.returncode}" if p.returncode is not None else "running"
                lines.append(f"{sid}: {status}")
            return ToolResult.ok("\n".join(lines) if lines else "(no processes)")

        case "write":
            if not session_id or session_id not in _procs:
                return ToolResult.error(f"process not found: {session_id}")
            if not data:
                return ToolResult.error("data is required for action=write")
            p = _procs[session_id]
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

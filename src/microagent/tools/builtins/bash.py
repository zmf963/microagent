"""bash builtin tool — execute shell commands via async subprocess.

Capability-family seam: a ``TerminalBackend`` (Local/Docker/SSH) can be
bound per-session via the ``bash_current_backend`` ContextVar. When bound,
bash delegates to it — swapping the backend migrates the whole capability
family (docker isolation, remote SSH execution) without touching the
tool. When unbound (default), the hardened local implementation runs:
incremental bounded reads, process-group kill, timeout with partial
output. The local implementation is deliberately NOT routed through
``LocalTerminal`` — that backend's communicate() buffers unbounded
output; the direct path keeps the 100KB cap.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import signal
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

_current_backend: contextvars.ContextVar = contextvars.ContextVar(
    "bash_current_backend", default=None
)


def set_backend(backend: object | None) -> None:
    """Bind a TerminalBackend (or None = hardened local path) for the
    current context. The runner binds its per-session backend in _settle.

    NOTE for library users: the runner rebinds on EVERY tool execution —
    a direct set_backend() call is overwritten by the runner's own
    terminal_backend (typically None) on the next bash call in a turn.
    To route a runner's bash permanently, construct it with
    ``SessionRunner(terminal_backend=...)``; use set_backend only for
    direct (runner-less) tool invocation.
    """
    _current_backend.set(backend)


def _kill_proc_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the process group so grandchildren die too (not just /bin/sh).

    Requires start_new_session=True at spawn. Falls back to proc.kill()
    if the process group can't be determined (already reaped, no perm).
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


@tool("bash", description="Execute a shell command and return its output.")
async def bash(
    command: Annotated[str, Field(description="The shell command to execute")],
    timeout: Annotated[int, Field(description="Timeout in seconds", ge=1, le=600)] = 120,
) -> ToolResult:
    backend = _current_backend.get()
    if backend is not None:
        return await _run_via_backend(backend, command, timeout)

    MAX_OUTPUT = 100_000  # prevent OOM from runaway output

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # new process group for clean kill
    )
    # Read stdout incrementally so we can capture partial output on timeout.
    chunks: list[bytes] = []
    total = 0

    async def _read_all() -> None:
        nonlocal total
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            if total < MAX_OUTPUT:
                chunks.append(chunk)
                total += len(chunk)

    try:
        try:
            await asyncio.wait_for(_read_all(), timeout=timeout)
        except TimeoutError:
            _kill_proc_group(proc)
            await proc.wait()
            partial = b"".join(chunks).decode("utf-8", errors="replace")
            return ToolResult.error(
                f"command timed out after {timeout}s\npartial output:\n{partial}"
            )

        await proc.wait()
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if len(output) > MAX_OUTPUT:
            # NOTE: the "N bytes beyond" figure only counts bytes that
            # survived collection — the reader loop may already have dropped
            # chunks once MAX_OUTPUT was exceeded, so N ≈ 0 on huge outputs.
            # Treat it as a hint, not an exact measurement.
            output = (
                output[:MAX_OUTPUT]
                + f"\n[truncated: {len(output) - MAX_OUTPUT} bytes beyond {MAX_OUTPUT} limit]"
            )
        exit_code = proc.returncode

        if exit_code != 0:
            output = f"{output}\n[exit code: {exit_code}]"
            return ToolResult.error(output)

        return ToolResult.ok(output if output else "(no output)")

    except BaseException:
        # CancelledError (budget exhausted, user Ctrl-C, runner.cancel())
        # is a BaseException — bare `except Exception` misses it, leaving
        # the subprocess orphaned. Kill before re-raising.
        _kill_proc_group(proc)
        try:
            await proc.wait()
        except Exception:
            pass
        raise


async def _run_via_backend(backend, command: str, timeout: int) -> ToolResult:
    """Route a bash call through a bound TerminalBackend.

    The backend runs in an isolated/remote environment (docker container,
    SSH host). Its result is translated into the bash tool's contract:
    non-zero exit or timeout → error ToolResult with the same suffix
    conventions as the local path.
    """
    try:
        result = await backend.run(command, timeout=timeout)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return ToolResult.error(f"bash backend failed: {e!r}")

    # stderr merge: append stderr as a marked section unless it is empty
    # or a byte-identical duplicate of stdout (a backend that already
    # merged streams). The old endswith() heuristic dropped a DISTINCT
    # stderr whenever stdout happened to end with the same text, and
    # merged stream-equal stderr as if distinct.
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if stderr and stderr != stdout and not stdout.endswith("\n" + stderr):
        output = f"{stdout}\n[stderr]\n{stderr}".strip()
    else:
        output = stdout

    if result.timed_out:
        return ToolResult.error(
            f"command timed out after {timeout}s\npartial output:\n{output}"
        )
    if result.exit_code != 0:
        return ToolResult.error(f"{output}\n[exit code: {result.exit_code}]")
    return ToolResult.ok(output if output else "(no output)")

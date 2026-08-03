"""bash builtin tool — execute shell commands via async subprocess."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


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

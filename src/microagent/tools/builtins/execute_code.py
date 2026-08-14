"""execute_code builtin tool — run Python code in a subprocess.

Code runs in a fresh subprocess with a configurable timeout. The
subprocess has full access to the Python stdlib and the filesystem;
it is NOT a sandbox.  Use with caution — the code runs with the
same privileges as the agent process.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


def _kill_proc_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the process group so grandchildren die too (not just python).

    Requires start_new_session=True at spawn (mirrors bash.py). Without it,
    user code that spawns its own subprocess (Popen([...])) leaves that
    grandchild running after the timeout kills only the python parent.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


@tool(
    "execute_code",
    description="Run Python code in a subprocess. Returns stdout. Timeout in seconds.",
)
async def execute_code(
    code: Annotated[str, Field(description="Python code to execute")],
    timeout: Annotated[
        float, Field(description="Max execution time in seconds", ge=0.1, le=300)
    ] = 60.0,
) -> ToolResult:
    if not code.strip():
        return ToolResult.error("code is required")

    MAX_OUTPUT = 100_000  # 100 KB cap — matches bash.py

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # New process group so a timeout/cancel can SIGKILL the whole
        # group — otherwise a Popen([...]) inside the user's code survives
        # as an orphan after we kill only the python parent.
        start_new_session=True,
    )

    # Stream stdout incrementally so a runaway producer (e.g.
    # `while True: print('x'*10**6)`) is bounded by MAX_OUTPUT in memory,
    # not just by the timeout. communicate() buffers the ENTIRE output
    # before returning, so it could OOM the agent process before the
    # timeout ever fires. Drop chunks once the budget is exceeded.
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
            try:
                await proc.wait()
            except Exception:
                pass
            return ToolResult.error(f"execution timed out after {timeout}s")

        await proc.wait()
        output = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if len(output) > MAX_OUTPUT:
            output = (
                output[:MAX_OUTPUT]
                + f"\n[truncated: {len(output) - MAX_OUTPUT} bytes beyond {MAX_OUTPUT} limit]"
            )
        if proc.returncode != 0:
            return ToolResult.error(
                f"exit code {proc.returncode}\n{output}"
                if output
                else f"exit code {proc.returncode}"
            )

        return ToolResult.ok(output if output else "(no output)")
    except BaseException:
        # CancelledError (interrupt, budget exhausted, Ctrl-C) is a
        # BaseException — bare `except Exception` misses it, leaving the
        # subprocess orphaned. Kill the whole group before re-raising
        # (mirrors bash.py).
        _kill_proc_group(proc)
        try:
            await proc.wait()
        except Exception:
            pass
        raise

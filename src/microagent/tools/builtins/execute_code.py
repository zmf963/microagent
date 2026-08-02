"""execute_code builtin tool — run Python code in a subprocess.

Code runs in a fresh subprocess with a configurable timeout. The
subprocess has full access to the Python stdlib and the filesystem;
it is NOT a sandbox.  Use with caution — the code runs with the
same privileges as the agent process.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


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

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return ToolResult.error(f"execution timed out after {timeout}s")

        output = stdout.decode("utf-8", errors="replace").strip()
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
    except Exception as e:
        return ToolResult.error(f"execution failed: {e!r}")

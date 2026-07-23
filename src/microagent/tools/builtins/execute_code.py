"""execute_code builtin tool — run Python code in a subprocess sandbox.

Code runs in a fresh subprocess with a timeout. Only stdlib is available
by default. The subprocess has no access to the agent's memory or tools.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool("execute_code", description="Run Python code in a subprocess. Returns stdout. Timeout in seconds.")
async def execute_code(
    code: Annotated[str, Field(description="Python code to execute")],
    timeout: Annotated[float, Field(description="Max execution time in seconds", ge=0.1, le=300)] = 60.0,
) -> ToolResult:
    if not code.strip():
        return ToolResult.error("code is required")

    import asyncio
    import sys

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return ToolResult.error(
                f"execution timed out after {timeout}s"
            )

        output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return ToolResult.error(
                f"exit code {proc.returncode}\n{output}" if output else f"exit code {proc.returncode}"
            )

        return ToolResult.ok(output if output else "(no output)")
    except Exception as e:
        return ToolResult.error(f"execution failed: {e!r}")

"""bash builtin tool — execute shell commands via async subprocess."""

from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool("bash", description="Execute a shell command and return its output.")
async def bash(
    command: Annotated[str, Field(description="The shell command to execute")],
    timeout: Annotated[int, Field(description="Timeout in seconds", ge=1, le=600)] = 120,
) -> ToolResult:
    MAX_OUTPUT = 100_000  # prevent OOM from runaway output

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # Read stdout incrementally so we can capture partial output on timeout.
        # Using communicate() would lose buffered data when wait_for cancels.
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
            await asyncio.wait_for(_read_all(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            partial = b"".join(chunks).decode("utf-8", errors="replace")
            return ToolResult.error(
                f"command timed out after {timeout}s\npartial output:\n{partial}"
            )

        await proc.wait()
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if len(output) > MAX_OUTPUT:
            output = (
                output[:MAX_OUTPUT]
                + f"\n[truncated: {len(output) - MAX_OUTPUT} bytes beyond {MAX_OUTPUT} limit]"
            )
        exit_code = proc.returncode

        if exit_code != 0:
            output = f"{output}\n[exit code: {exit_code}]"
            return ToolResult.error(output)

        return ToolResult.ok(output if output else "(no output)")

    except Exception as e:
        return ToolResult.error(f"failed to execute command: {e!r}")

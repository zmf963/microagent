"""git builtin tool — safe git operations with subcommand whitelist.

Only read-only subcommands + commit/add are allowed. Write operations
like push/reset/force-push are blocked.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

_GIT_WHITELIST = frozenset({
    "status",
    "diff",
    "log",
    "show",
    "branch",
    "commit",
    "add",
})


@tool(
    "git",
    description="Run safe git subcommands (status, diff, log, show, branch, commit, add). "
    "Write operations (push, reset, force-push) are not allowed.",
)
async def git(
    subcommand: Annotated[str, Field(description="Git subcommand: status, diff, log, show, branch, commit, add")],
    repo_path: Annotated[str, Field(description="Path to the git repository")] = ".",
    args: Annotated[str, Field(description="Additional arguments to pass to the subcommand")] = "",
) -> ToolResult:
    if subcommand not in _GIT_WHITELIST:
        return ToolResult.error(
            f"git subcommand '{subcommand}' is not allowed. "
            f"Allowed: {', '.join(sorted(_GIT_WHITELIST))}"
        )

    cmd_parts = ["git", "-C", repo_path, subcommand]
    if args:
        cmd_parts.extend(args.split())

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return ToolResult.error(f"git {subcommand} failed (exit {proc.returncode}): {err}")

        return ToolResult.ok(output if output.strip() else "(no output)")
    except FileNotFoundError:
        return ToolResult.error("git not found — is git installed?")
    except Exception as e:
        return ToolResult.error(f"git {subcommand} failed: {e!r}")

"""grep builtin tool — search file contents using regex."""

from __future__ import annotations

import asyncio
import re
import signal
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# Per-file size cap — reading a 5 GB log entirely (to scan for \x00 and
# decode) exhausts memory even though only matching lines are needed.
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

# SIGALRM-based regex timeout (Unix only). asyncio runs in the main thread,
# so signal.alarm is safe to set from a tool. Catastrophic-backtracking
# patterns (e.g. (a+)+b) on a long line would otherwise hang the loop —
# a thread-based timeout doesn't work because the GIL prevents the search
# thread from being interrupted.
_HAS_SIGALRM = hasattr(signal, "SIGALRM")


class _RegexTimeout(Exception):
    """Raised when a regex search exceeds the alarm deadline."""


def _search_with_alarm(regex, line: str, seconds: int = 5):
    """Search with a SIGALRM deadline (Unix, main thread only).

    Returns the Match or None (None also means "timed out / not available").

    Limitations (inherent to SIGALRM, documented here):
      - Main thread only: signal.signal() raises ValueError when asyncio
        runs in a non-main thread (embed scenario). Falls back to an
        unprotected search in that case.
      - Not concurrency-safe: SIGALRM is process-global, so two concurrent
        grep calls inside one TaskGroup would clobber each other's alarm.
        The runner dispatches tool calls concurrently, but in practice a
        single agent rarely issues two greps in one turn; if it does, the
        worst case is one grep's line runs without timeout protection.
    """
    if not _HAS_SIGALRM:
        return regex.search(line)
    try:
        old_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(_RegexTimeout()))
        signal.alarm(seconds)
        return regex.search(line)
    except _RegexTimeout:
        return None
    except ValueError:
        # signal.signal() raises ValueError outside the main thread.
        # Fall back to an unprotected search rather than crashing grep.
        return regex.search(line)
    finally:
        try:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        except (ValueError, OSError):
            pass


@tool(
    "grep", description="Search file contents by regex. Returns matching lines with line numbers."
)
async def grep(
    pattern: Annotated[str, Field(description="Regular expression pattern to search for")],
    path: Annotated[str, Field(description="Directory or file to search in")] = ".",
    glob: Annotated[str, Field(description="File name glob pattern (e.g. '*.py')")] = "**/*",
    max_results: Annotated[int, Field(description="Maximum matches to return", ge=1, le=500)] = 50,
) -> ToolResult:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return ToolResult.error(f"invalid regex: {e}")

    root = Path(path)
    if not root.exists():
        return ToolResult.error(f"path not found: {path}")

    matches: list[str] = []

    if root.is_file():
        files = [root]
    else:
        files = sorted(root.glob(glob))

    for fpath in files:
        if not fpath.is_file():
            continue
        # Skip oversized files before reading.
        try:
            if fpath.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        # Skip binary files + read in a thread (network FS safety).
        try:
            raw = await asyncio.to_thread(fpath.read_bytes)
            if b"\x00" in raw:
                continue
            lines = raw.decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for i, line in enumerate(lines, 1):
            hit = _search_with_alarm(regex, line)
            if hit:
                rel = str(fpath)
                matches.append(f"{rel}:{i}: {line.strip()}")
                if len(matches) >= max_results:
                    matches.append(f"[truncated at {max_results} results]")
                    return ToolResult.ok("\n".join(matches))

    if not matches:
        return ToolResult.ok("(no matches)")

    return ToolResult.ok("\n".join(matches))

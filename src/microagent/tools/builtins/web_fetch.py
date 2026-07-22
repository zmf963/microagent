"""web_fetch builtin tool — fetch a URL and return text content."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool("web_fetch", description="Fetch a URL and return its text content.")
async def web_fetch(
    url: Annotated[str, Field(description="The URL to fetch")],
    timeout: Annotated[int, Field(description="Timeout in seconds", ge=1, le=60)] = 30,
) -> ToolResult:
    try:
        import httpx
    except ImportError:
        return ToolResult.error("httpx not installed. Install with: pip install httpx")

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        content = resp.text
        max_chars = 10_000
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n[truncated at {max_chars} chars]"

        return ToolResult.ok(content)

    except httpx.HTTPStatusError as e:
        return ToolResult.error(f"HTTP {e.response.status_code}: {e.response.reason_phrase}")
    except Exception as e:
        return ToolResult.error(f"fetch failed: {e!r}")

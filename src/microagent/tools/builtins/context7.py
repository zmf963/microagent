"""context7 builtin tool — fetch up-to-date library/framework documentation.

Uses the Context7 API (context7.com) to get relevant documentation
snippets for libraries and frameworks.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool(
    "context7",
    description="Fetch up-to-date library/framework docs from Context7. Use for APIs, config, or patterns you don't know.",
)
async def context7(
    query: Annotated[
        str,
        Field(
            description="What library/API/pattern to look up (e.g. 'pydantic v2 model validator')"
        ),
    ],
    library: Annotated[
        str, Field(description="Library name (e.g. 'fastapi', 'pydantic', 'react')")
    ] = "",
    max_results: Annotated[int, Field(description="Maximum results", ge=1, le=10)] = 5,
) -> ToolResult:
    if not query.strip():
        return ToolResult.error("query is required")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Stream the body so a malicious/buggy endpoint returning a huge
            # JSON payload can't OOM the agent before _parse_results truncates.
            max_bytes = 2 * 1024 * 1024  # 2 MB ceiling
            chunks: list[bytes] = []
            total = 0
            async with client.stream(
                "POST",
                "https://context7.com/api/query",
                json={"query": query, "library": library, "n": max_results},
                headers={"Content-Type": "application/json"},
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    if total >= max_bytes:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            truncated = total >= max_bytes
            try:
                data = httpx.Response(
                    status_code=200, content=b"".join(chunks)
                ).json()
            except Exception:
                if not truncated:
                    raise
                # The 2MB cap cut the payload mid-JSON — unparseable by
                # definition. Report it as an ERROR, not a success: a
                # syntactically broken JSON fragment with is_error=False
                # would be treated as trustworthy documentation by the
                # model (previously returned as ok()).
                return ToolResult.error(
                    "context7 response truncated at 2MB — partial JSON is "
                    "not parseable; narrow the query or reduce max_results"
                )
    except Exception as e:
        return ToolResult.error(f"context7 query failed: {e!r}")

    return ToolResult.ok(_parse_results(data, max_results))


def _parse_results(data: dict, max_results: int) -> str:
    """Parse Context7 API response into readable text."""
    results = data.get("results", [])
    if not results:
        return "(no results)"

    lines = []
    for i, r in enumerate(results[:max_results], 1):
        # A malformed-but-parseable payload ({"results": ["oops"]}) used
        # to raise AttributeError out of the tool; treat non-dict entries
        # as skippable noise instead.
        if not isinstance(r, dict):
            continue
        title = r.get("title", "Untitled")
        snippet = r.get("snippet", "")
        url = r.get("url", "")
        lib = r.get("library", "")
        meta = f" [{lib}]" if lib else ""
        lines.append(f"{i}. {title}{meta}")
        if snippet:
            lines.append(f"   {snippet}")
        if url:
            lines.append(f"   {url}")
        lines.append("")

    return "\n".join(lines).strip() or "(no results)"

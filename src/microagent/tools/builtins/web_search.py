"""web_search builtin tool — search the web via DuckDuckGo HTML.

Uses DuckDuckGo's lite HTML interface (no API key required).
Returns structured results: title, URL, snippet.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


@tool("web_search", description="Search the web via DuckDuckGo. Returns titles, URLs, and snippets.")
async def web_search(
    query: Annotated[str, Field(description="Search query")],
    max_results: Annotated[int, Field(description="Maximum results", ge=1, le=20)] = 5,
) -> ToolResult:
    if not query.strip():
        return ToolResult.error("query is required")

    import asyncio
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers={"User-Agent": "MicroAgent/0.1"},
            )
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        return ToolResult.error(f"search failed: {e!r}")

    # Parse DuckDuckGo lite HTML results
    results = _parse_ddg_lite(html, max_results)

    if not results:
        return ToolResult.ok("(no results)")

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   {r['url']}")
        lines.append(f"   {r['snippet']}")
        lines.append("")
    return ToolResult.ok("\n".join(lines).strip())


def _parse_ddg_lite(html: str, max_results: int) -> list[dict]:
    """Parse DuckDuckGo lite HTML into structured results."""
    results = []

    # DDG lite format: <a href="URL">title</a> ... <span class="link-text">snippet</span>
    # Simpler approach: extract all <a> with href containing "//" and their following text
    links = re.findall(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
    snippets = re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL)

    for i, (url, title) in enumerate(links):
        if i >= max_results:
            break
        # Clean HTML from title
        title = re.sub(r'<[^>]+>', '', title).strip()
        if not title or not url:
            continue
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
        results.append({"title": title, "url": url, "snippet": snippet})

    return results

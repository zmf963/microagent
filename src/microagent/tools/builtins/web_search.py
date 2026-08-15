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


@tool(
    "web_search", description="Search the web via DuckDuckGo. Returns titles, URLs, and snippets."
)
async def web_search(
    query: Annotated[str, Field(description="Search query")],
    max_results: Annotated[int, Field(description="Maximum results", ge=1, le=20)] = 5,
) -> ToolResult:
    if not query.strip():
        return ToolResult.error("query is required")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers={"User-Agent": "MicroAgent/0.1"},
            )
            resp.raise_for_status()
            # Cap the body: a hostile/misconfigured upstream returning a
            # multi-MB page previously fed re.findall across the whole
            # string. 2 MB covers any realistic result page.
            html = resp.text[:2_000_000]
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
    # Chrome links (logo, feedback, spread, the ddg homepage) appear
    # between result anchors — pairing the N-th anchor with the N-th
    # snippet misaligned every snippet after such a link. Filter obvious
    # chrome and consume snippets in emission order (not raw index), so
    # skipped empty titles no longer shift the pairing either.
    _CHROME_LINK_RE = re.compile(
        r"(^https?://(www\.)?duckduckgo\.com/?$|/feedback|spread\.duckduckgo\.com)"
    )
    links = re.findall(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
    result_links = [
        (url, title) for url, title in links if not _CHROME_LINK_RE.search(url)
    ]
    snippets = re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL)

    emitted = 0
    for url, title in result_links:
        if emitted >= max_results:
            break
        # Clean HTML from title
        title = re.sub(r"<[^>]+>", "", title).strip()
        if not title or not url:
            continue
        # Snippets align 1:1 with RESULT links (not chrome links) — but
        # tolerate drift (skipped empty titles etc.) by consuming the
        # next available snippet instead of indexing by raw position.
        snippet = ""
        if snippets:
            snippet = re.sub(r"<[^>]+>", "", snippets.pop(0)).strip()
        results.append({"title": title, "url": url, "snippet": snippet})
        emitted += 1

    return results

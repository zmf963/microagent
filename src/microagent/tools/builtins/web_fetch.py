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
    # Validate URL first — SSRF protection
    from urllib.parse import urlparse
    import ipaddress

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ToolResult.error(
            f"unsupported URL scheme: {parsed.scheme!r}. Only http/https allowed."
        )

    host = parsed.hostname
    if not host:
        return ToolResult.error(f"invalid URL: no hostname found in {url!r}")

    # Block internal/private IPs and localhost
    blocked_ranges = [
        ipaddress.IPv4Network("127.0.0.0/8"),
        ipaddress.IPv4Network("10.0.0.0/8"),
        ipaddress.IPv4Network("172.16.0.0/12"),
        ipaddress.IPv4Network("192.168.0.0/16"),
        ipaddress.IPv4Network("169.254.0.0/16"),
        ipaddress.IPv6Network("::1/128"),
        ipaddress.IPv6Network("fc00::/7"),
    ]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # hostname — IP check not applicable, proceed
        pass
    else:
        for net in blocked_ranges:
            if addr in net:
                return ToolResult.error(
                    f"blocked: {host!r} is a private/internal address (SSRF protection)"
                )

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

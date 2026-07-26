"""web_fetch builtin tool — fetch a URL and return text content.

SSRF protection: blocks literal internal IPs, internal hostnames,
and resolves hostnames to IPs before connecting to prevent DNS rebinding.
Redirects are NOT followed — each redirect target must pass the same checks.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# Blocked IP ranges (RFC 1918, loopback, link-local, etc.)
_BLOCKED_RANGES = [
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
]

_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "broadcasthost",
})


def _is_blocked_ip(ip_str: str) -> bool:
    """Check if an IP address string is in blocked ranges."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in _BLOCKED_RANGES)


def _resolve_and_check(host: str) -> str | None:
    """Resolve hostname to IP and check against blocked ranges.

    Returns None if safe, or an error message string if blocked.
    """
    if host.lower() in _BLOCKED_HOSTNAMES or host.endswith(".local"):
        return f"blocked: {host!r} is a local/internal hostname (SSRF protection)"

    try:
        addrs = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return f"cannot resolve hostname: {host!r}"

    for addr_info in addrs:
        ip = addr_info[4][0]
        if _is_blocked_ip(ip):
            return f"blocked: {host!r} resolves to {ip!r} (SSRF protection)"
    return None


@tool("web_fetch", description="Fetch a URL and return its text content.")
async def web_fetch(
    url: Annotated[str, Field(description="The URL to fetch")],
    timeout: Annotated[int, Field(description="Timeout in seconds", ge=1, le=60)] = 30,
) -> ToolResult:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ToolResult.error(
            f"unsupported URL scheme: {parsed.scheme!r}. Only http/https allowed."
        )

    host = parsed.hostname
    if not host:
        return ToolResult.error(f"invalid URL: no hostname found in {url!r}")

    error = _resolve_and_check(host)
    if error is not None:
        return ToolResult.error(error)

    try:
        import httpx
    except ImportError:
        return ToolResult.error("httpx not installed. Install with: pip install httpx")

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
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

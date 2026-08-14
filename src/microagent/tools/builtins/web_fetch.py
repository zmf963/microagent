"""web_fetch builtin tool — fetch a URL and return text content.

SSRF protection: blocks literal internal IPs, internal hostnames,
and resolves hostnames to IPs before connecting. Redirects are NOT
followed: a 3xx response is returned as-is (usually an empty body), so a
redirect can never smuggle the agent to an unchecked target.

Known limitation: the resolve-then-connect check has a TOCTOU window —
httpx re-resolves the hostname at connect time, so an attacker-controlled
domain with split-horizon DNS (public answer to the check query,
internal answer to httpx's query) could in theory bypass the blocklist.
Closing this requires pinning the validated IP with an SNI/Host override;
kept out of scope for now because it needs a custom httpx transport.
"""

from __future__ import annotations

import asyncio
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
    # CGNAT (RFC 6598) — used by Tailscale tailnets and some ISPs;
    # services here are internal even though the range is not RFC 1918.
    ipaddress.IPv4Network("100.64.0.0/10"),
    # Benchmarking (RFC 2544) — used internally by some devices/ISPs.
    ipaddress.IPv4Network("198.18.0.0/15"),
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
    """Check if an IP address string is in blocked ranges.

    Also catches IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1) that
    would otherwise bypass the IPv4 blocklist because ip_address() reports
    them as family=6.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # Reject any IPv4-mapped IPv6 address whose mapped v4 is internal.
    # This covers ::ffff:127.0.0.1, ::ffff:169.254.169.254 (AWS metadata), etc.
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return any(mapped in net for net in _BLOCKED_RANGES if isinstance(net, ipaddress.IPv4Network))
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

    error = await asyncio.to_thread(_resolve_and_check, host)
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
            # Stream the body so a multi-GB response cannot OOM the agent
            # before the truncation at max_chars runs.
            max_bytes = 2 * 1024 * 1024  # 2 MB hard ceiling
            chunks: list[bytes] = []
            total = 0
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    if total >= max_bytes:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            content = b"".join(chunks).decode("utf-8", errors="replace")

        max_chars = 10_000
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n[truncated at {max_chars} chars]"

        return ToolResult.ok(content)

    except httpx.HTTPStatusError as e:
        return ToolResult.error(f"HTTP {e.response.status_code}: {e.response.reason_phrase}")
    except Exception as e:
        return ToolResult.error(f"fetch failed: {e!r}")

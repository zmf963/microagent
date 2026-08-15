"""LLM stream idle watchdog — bounded waits on a silent stream.

deepseek-harness parity: streamIdleTimeoutMs (default 5min) aborts a
stream that produces no event for too long — a hung gateway otherwise
blocks the turn forever (no exception, no completion). Wraps any async
iterator of events and raises ``IdleTimeoutError`` when the gap between
consecutive events exceeds the limit.

Timeout of 0 disables the watchdog.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TypeVar

T = TypeVar("T")


class IdleTimeoutError(TimeoutError):
    """Raised when the LLM stream stalls beyond the idle limit."""


async def watch_idle(
    stream: AsyncIterator[T],
    timeout_seconds: float,
) -> AsyncIterator[T]:
    """Yield events, raising IdleTimeoutError after ``timeout_seconds``
    of silence. A zero timeout disables the watchdog (pure passthrough).

    On timeout/cancel the wrapped stream is aclose()d (cancellation-
    shielded, best-effort): an abandoned generator holds the live httpx
    response and its pooled connection until GC — each watchdog firing
    permanently shrinks the pool by one connection.
    """
    if timeout_seconds <= 0:
        async for event in stream:
            yield event
        return

    import asyncio

    stream_iter = stream.__aiter__()
    try:
        while True:
            try:
                event = await asyncio.wait_for(
                    stream_iter.__anext__(), timeout=timeout_seconds
                )
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                raise IdleTimeoutError(
                    f"LLM stream idle for {timeout_seconds:g}s — no events received"
                ) from None
            yield event
    finally:
        # Close the abandoned generator so the underlying httpx stream
        # releases its pooled connection. Shield against cancellation —
        # interrupt must not abort the cleanup and re-leak. Best-effort:
        # a misbehaving generator's aclose raising must not mask the
        # original exception.
        close = getattr(stream_iter, "aclose", None)
        if close is not None:
            try:
                await asyncio.shield(close())
            except BaseException:
                pass

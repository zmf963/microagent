"""EventBus — lightweight pub/sub for observing agent events.

Observers only: handlers cannot modify payloads. Exceptions are
swallowed to prevent observer failures from breaking the main loop.
Supports both sync and async callbacks transparently.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class EventBus:
    """Simple pub/sub event bus. Observers only — no transform capability."""

    subscribers: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)

    def on(self, event: str, cb: Callable[..., Any]) -> None:
        """Register a callback for an event."""
        self.subscribers.setdefault(event, []).append(cb)

    async def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Fire an event. All callbacks are called in order.
        Exceptions are swallowed — observer failures don't block the main loop.
        """
        for cb in self.subscribers.get(event, []):
            try:
                result = cb(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass  # observer errors must not propagate

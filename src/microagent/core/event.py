"""EventBus — lightweight pub/sub for observing agent events.

Observers only: handlers cannot modify payloads. Exceptions are
logged but swallowed to prevent observer failures from breaking the main loop.
Supports both sync and async callbacks transparently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EventBus:
    """Simple pub/sub event bus. Observers only — no transform capability."""

    subscribers: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)

    def on(self, event: str, cb: Callable[..., Any]) -> None:
        """Register a callback for an event. Duplicate registrations of
        the SAME callback for the SAME event are ignored (re-registering
        previously fired it once per duplicate)."""
        subs = self.subscribers.setdefault(event, [])
        if cb not in subs:
            subs.append(cb)

    def off(self, event: str, cb: Callable[..., Any]) -> None:
        """Unregister a callback. No-op if it was never registered.

        Long-lived agents (the target audience) need to release observer
        references; without off() subscribers only ever accumulate.
        """
        subs = self.subscribers.get(event, [])
        try:
            subs.remove(cb)
        except ValueError:
            pass
        if not subs and event in self.subscribers:
            del self.subscribers[event]

    async def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Fire an event. Sync callbacks run in order; async callbacks run
        concurrently — one slow async observer (e.g. a remote logging hook)
        must not stall the main loop at the turn_complete emit point.
        Exceptions are swallowed — observer failures don't block the main loop.
        """
        pending = []
        for cb in self.subscribers.get(event, []):
            try:
                result = cb(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    # Wrap in a Task: a gather() of raw coroutines cancels
                    # EVERY other pending coroutine when one of them is
                    # externally cancelled — a slow observer's work was
                    # silently discarded. Tasks isolate each observer.
                    pending.append(asyncio.create_task(result))
            except BaseException:
                # BaseException (not just Exception): a KeyboardInterrupt/
                # SystemExit raised by a sync observer must not terminate
                # the main loop at the emit point.
                logger.warning(
                    "EventBus observer failed for event %s", event, exc_info=True
                )
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    logger.warning(
                        "EventBus observer failed for event %s", event, exc_info=r
                    )

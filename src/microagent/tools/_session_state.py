"""Shared helper for per-session tool state via ContextVars.

Each tool that holds per-session state (process registry, browser page,
LSP clients, etc.) declares an identical pattern:

    _current_X: contextvars.ContextVar[X | None] = contextvars.ContextVar(
        "tool_current_X", default=None,
    )

    def _get_X() -> X:
        v = _current_X.get()
        if v is None:
            v = X()
            _current_X.set(v)
        return v

This module factors that into one factory so each tool module drops from
~6 LOC of boilerplate to ~2 LOC, and the lazy-create-and-set behavior
stays consistent across tools.
"""

from __future__ import annotations

import contextvars
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


def session_state(
    name: str, factory: Callable[[], T]
) -> tuple[contextvars.ContextVar[T | None], Callable[[], T]]:
    """Create a per-session ContextVar + a lazy getter.

    Args:
        name: stable identifier for the ContextVar (must be unique).
        factory: zero-arg callable that builds a fresh state object on
            first access (when the ContextVar's default None is read).

    Returns:
        (var, get) where:
          - var: the ContextVar (use var.set(...) in SessionRunner._settle
            to bind the runner's per-session state)
          - get: a callable that returns the current state, lazily
            creating + setting it via ``factory`` if None.

    Example::

        _current_registry, _get_registry = session_state(
            "process_current_registry", ProcRegistry,
        )
    """
    var: contextvars.ContextVar[T | None] = contextvars.ContextVar(name, default=None)

    def get() -> T:
        v = var.get()
        if v is None:
            v = factory()
            var.set(v)
        return v

    return var, get

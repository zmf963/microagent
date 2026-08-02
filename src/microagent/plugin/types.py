"""Extension point Protocols for MicroAgent.

These are the three seams where users can extend agent behavior:
- PreLLMHook: transform the system prompt string before the LLM call.
- ToolHook: intercept tool calls (before/after execution).
- ContextSource: inject extra content into the USER message (per ADR-0005,
  the system prompt is frozen; context is appended to the user turn).

All three are PEP 544 Protocols — no base class inheritance required.

Note: all three Protocols carry a ``ctx: Any`` parameter that is currently
always ``None`` at call sites (runner.py). It is a reserved seam for a
future TurnContext object; ignore it for now.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..core.types import ToolCall, ToolResult


class PreLLMHook(Protocol):
    """Transform the system prompt before the LLM call. Return the new string.

    Called with the current system prompt (a str). The return value
    becomes the system prompt sent to the LLM. ``ctx`` is reserved
    (currently always None)."""

    async def __call__(self, ctx: Any) -> Any: ...


class ToolHook(Protocol):
    """Intercept tool calls: before returns None to deny, or a modified call.
    after can transform the result. ``ctx`` is reserved (currently None)."""

    async def before(self, call: ToolCall, ctx: Any) -> ToolCall | None: ...

    async def after(self, call: ToolCall, result: ToolResult, ctx: Any) -> ToolResult: ...


class ContextSource(Protocol):
    """Inject extra content into the user message (per ADR-0005). Returns a
    string to append. ``ctx`` is reserved (currently None)."""

    async def contribute(self, ctx: Any) -> str: ...

"""Extension point Protocols for MicroAgent.

These are the three seams where users can extend agent behavior:
- PreLLMHook: transform TurnContext before LLM call
- ToolHook: intercept tool calls (before/after execution)
- ContextSource: inject extra content into system prompt

All three are PEP 544 Protocols — no base class inheritance required.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..core.types import ToolCall, ToolResult


class PreLLMHook(Protocol):
    """Transform TurnContext before LLM call. Return modified ctx."""

    async def __call__(self, ctx: Any) -> Any: ...


class ToolHook(Protocol):
    """Intercept tool calls: before returns None to deny, or modified call.
    after can transform the result."""

    async def before(self, call: ToolCall, ctx: Any) -> ToolCall | None: ...

    async def after(self, call: ToolCall, result: ToolResult, ctx: Any) -> ToolResult: ...


class ContextSource(Protocol):
    """Inject extra content into system prompt. Returns a string to append."""

    async def contribute(self, ctx: Any) -> str: ...

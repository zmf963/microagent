"""Core type definitions: Message, ToolCall, ToolResult, Events.

These are the fundamental data structures exchanged between
SessionRunner, LLMClient, and Tool implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union


# ---------------------------------------------------------------------------
# Message — the universal LLM message format
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage from an LLM response."""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class Message:
    """LLM message (user / assistant / tool roles unified in one type).

    For role='tool', ``tool_call_id`` must be set to associate the
    result with the originating tool call.
    """
    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    usage: Usage | None = None
    is_error: bool = False

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=text)

    @classmethod
    def assistant(cls, text: str, *, tool_calls: tuple[ToolCall, ...] = (),
                  usage: Usage | None = None) -> Message:
        return cls(role="assistant", content=text, tool_calls=tool_calls, usage=usage)

    @classmethod
    def tool_result(cls, result: ToolResult, *, tool_call_id: str) -> Message:
        """Build a role='tool' message from a ToolResult.

        ``tool_call_id`` is required — the LLM API enforces it.
        """
        return cls(role="tool", content=result.content, tool_call_id=tool_call_id,
                   is_error=result.is_error)

    def to_openai_dict(self) -> dict[str, Any]:
        """Convert to the dict format expected by the openai SDK."""
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_openai_dict() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d


# ---------------------------------------------------------------------------
# ToolCall / ToolResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single tool invocation requested by the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]

    def to_openai_dict(self) -> dict[str, Any]:
        import json
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of executing a tool."""
    content: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None

    @classmethod
    def ok(cls, content: str) -> ToolResult:
        return cls(content=content)

    @classmethod
    def error(cls, msg: str) -> ToolResult:
        return cls(content=msg, is_error=True)

    @classmethod
    def denied(cls, reason: str) -> ToolResult:
        return cls(content=f"denied: {reason}", is_error=True,
                   metadata={"denied": True})


# ---------------------------------------------------------------------------
# Events — yielded by SessionRunner.run_turn()
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TextDelta:
    """Incremental text output from the LLM.

    kind: 'thinking' (reasoning/CoT) or 'content' (final response)
    """
    text: str
    kind: str = "content"


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """A complete tool call (emitted after all deltas are accumulated)."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultDelta:
    """Result of a tool execution."""
    id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ToolProgressDelta:
    """Incremental output from a running tool — streamed in real time.

    Tools that support streaming (terminal, browser, etc.) yield
    these deltas while executing, so the user sees live progress
    instead of waiting for the full result.
    """
    id: str
    name: str
    text: str


@dataclass(frozen=True, slots=True)
class TurnComplete:
    """The turn finished with a text response."""
    content: str


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """The turn ended without a normal response."""
    reason: str


Event = Union[TextDelta, ToolCallDelta, ToolProgressDelta, ToolResultDelta, TurnComplete, TurnFailed]

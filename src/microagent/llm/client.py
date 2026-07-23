"""LLM abstraction: LLMConfig, LLMClient Protocol, OpenAIChatClient.

Only OpenAI Chat Completions API format is supported (``/v1/chat/completions``).
The openai SDK v2 handles SSE parsing, tool-call delta accumulation, and
retries. Different backends (vLLM, Ollama, DeepSeek, etc.) are selected
purely by ``base_url`` — no code-level provider adapters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from ..core.types import (
    Message, ToolCall, TextDelta, ToolCallDelta, Usage,
)


# ---------------------------------------------------------------------------
# Model pricing (USD per 1M tokens)
# ---------------------------------------------------------------------------

_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # (input_price_per_1M, output_price_per_1M)
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku": (0.25, 1.25),
    "deepseek-v3": (0.27, 1.10),
    "deepseek-r1": (0.55, 2.19),
    "glm-4": (0.50, 0.50),
    "oc-d4f": (0.0, 0.0),
    "tx-d4p": (0.0, 0.0),
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts and model pricing."""
    prices = _MODEL_PRICING.get(model)
    if prices is None:
        for prefix, p in _MODEL_PRICING.items():
            if model.startswith(prefix):
                prices = p
                break
    if prices is None:
        return 0.0
    input_price, output_price = prices
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


# ---------------------------------------------------------------------------
# LLMConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LLMConfig:
    """OpenAI-compatible LLM configuration: base_url + api_key + model."""
    base_url: str
    api_key: str
    model: str

    @classmethod
    def default(cls) -> LLMConfig:
        return cls(
            base_url="https://api.openai.com/v1",
            api_key="",
            model="gpt-4o",
        )


# ---------------------------------------------------------------------------
# Stream events produced by LLMClient.stream()
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StreamDone:
    usage: Usage
    stop_reason: str


StreamEvent = TextDelta | ToolCallDelta | Usage | StreamDone


# ---------------------------------------------------------------------------
# LLMClient Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMClient(Protocol):
    config: LLMConfig

    async def stream(
        self, *,
        system: str,
        messages: tuple[Message, ...],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    def for_model(self, model: str) -> LLMClient: ...


# ---------------------------------------------------------------------------
# OpenAIChatClient — the only built-in implementation
# ---------------------------------------------------------------------------

class OpenAIChatClient:
    """Uses the openai SDK v2 AsyncOpenAI client.

    Handles tool-call delta accumulation internally so that the caller
    (SessionRunner) only sees complete ToolCallDelta events.
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        # Lazily create the SDK client (deferred so tests can mock)
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
            )
        return self._client

    def for_model(self, model: str) -> OpenAIChatClient:
        return OpenAIChatClient(replace(self.config, model=model))

    async def stream(
        self, *,
        system: str,
        messages: tuple[Message, ...],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        client = self._get_client()

        # Build OpenAI messages: system first, then conversation
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        oai_messages.extend(m.to_openai_dict() for m in messages)

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": oai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        stream = await client.chat.completions.create(**kwargs)

        # Accumulate tool_call fragments by index
        tool_acc: dict[int, dict[str, Any]] = {}
        usage: Usage | None = None
        stop_reason = "stop"

        async for chunk in stream:
            # Capture usage (arrives in the final chunk)
            if hasattr(chunk, "usage") and chunk.usage is not None:
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                    cost_usd=_estimate_cost(
                        self.config.model,
                        chunk.usage.prompt_tokens or 0,
                        chunk.usage.completion_tokens or 0,
                    ),
                )
                continue

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            finish = chunk.choices[0].finish_reason

            if finish:
                stop_reason = finish

            # Text delta — yield immediately for streaming UX
            if delta and delta.content:
                yield TextDelta(text=delta.content, kind="content")

            # Reasoning content (CoT / thinking) — some models expose this
            if delta and hasattr(delta, "reasoning_content") and delta.reasoning_content:
                yield TextDelta(text=delta.reasoning_content, kind="thinking")

            # Tool call deltas — accumulate by index
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_acc:
                        tool_acc[idx] = {
                            "id": tc.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    if tc.id:
                        tool_acc[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_acc[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_acc[idx]["arguments"] += tc.function.arguments

        # After stream ends, yield complete tool calls
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            try:
                args = json.loads(acc["arguments"]) if acc["arguments"] else {}
            except json.JSONDecodeError:
                args = {"_raw": acc["arguments"]}
            yield ToolCallDelta(
                id=acc["id"],
                name=acc["name"],
                arguments=args,
            )

        # Yield usage + done
        final_usage = usage or Usage()
        yield final_usage
        yield StreamDone(usage=final_usage, stop_reason=stop_reason)

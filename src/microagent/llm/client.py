"""LLM abstraction: LLMConfig, LLMClient Protocol, OpenAIChatClient.

Only OpenAI Chat Completions API format is supported (``/v1/chat/completions``).
The openai SDK v2 handles SSE parsing, tool-call delta accumulation, and
retries. Different backends (vLLM, Ollama, DeepSeek, etc.) are selected
purely by ``base_url`` — no code-level provider adapters.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .pool import CredentialPool

from ..core.types import (
    Message,
    TextDelta,
    ToolCallDelta,
    Usage,
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
    "tx-d4f": (0.0, 0.0),
    "tx-d4p": (0.0, 0.0),
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts and model pricing.

    Falls back to a conservative $0.50 / 1M tokens for unknown models
    rather than returning 0.0 (which would make Budget cost tracking
    silently ineffective).
    """
    prices = _MODEL_PRICING.get(model)
    if prices is None:
        for prefix, p in _MODEL_PRICING.items():
            if model.startswith(prefix):
                prices = p
                break
    if prices is None:
        # Conservative fallback — prevents cost-unaware Budget runaway
        prices = (0.50, 0.50)
    input_price, output_price = prices
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


# ---------------------------------------------------------------------------
# Model context windows (tokens) — for adaptive compression thresholds
# ---------------------------------------------------------------------------

_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    "claude-sonnet-4": 200_000,
    "claude-haiku": 200_000,
    "claude-opus-4": 200_000,
    "deepseek-v3": 128_000,
    "deepseek-r1": 128_000,
    "glm-4": 128_000,
    "oc-d4f": 200_000,
    "tx-d4f": 200_000,
    "tx-d4p": 200_000,
}


def get_context_window(model: str) -> int:
    """Return the context window size for a model (prefix match)."""
    for prefix, window in _MODEL_CONTEXT_WINDOWS.items():
        if model.startswith(prefix):
            return window
    return 128_000  # safe default


# ---------------------------------------------------------------------------
# Backoff retry configuration
# ---------------------------------------------------------------------------

MAX_BACKOFF_RETRIES = 3
BACKOFF_BASE = 2.0  # exponential base
BACKOFF_JITTER = 0.25  # ±25% jitter


# ---------------------------------------------------------------------------
# LLMConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """OpenAI-compatible LLM configuration.

    Fields:
        base_url: API endpoint (e.g. https://api.openai.com/v1)
        api_key: Authentication key
        model: Model identifier
        reasoning_effort: For o-series models — 'low', 'medium', 'high'
        service_tier: OpenAI service tier — 'auto', 'default', 'flex'
        auxiliary_model: Optional cheaper/faster model for compression
    """

    base_url: str
    api_key: str
    model: str
    reasoning_effort: str | None = None
    service_tier: str | None = None
    auxiliary_model: str | None = None

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
    """Stream finished — final usage + stop reason."""

    usage: Usage
    stop_reason: str


StreamEvent = TextDelta | ToolCallDelta | Usage | StreamDone


# ---------------------------------------------------------------------------
# LLMClient Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """LLM provider interface — stream responses + model forking."""

    config: LLMConfig

    async def stream(
        self,
        *,
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

    Supports CredentialPool for API key rotation on failure.
    """

    def __init__(self, config: LLMConfig, pool: CredentialPool | None = None):
        self.config = config
        self.pool = pool
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
            )
        return self._client

    async def _create_with_backoff(self, kwargs: dict[str, Any]):
        """Call chat.completions.create with jittered exponential backoff.

        Retries on rate-limit/timeout/connection/5xx errors up to
        MAX_BACKOFF_RETRIES times. Auth errors (401/403) are NOT retried
        here — they fall through to the credential pool rotation in stream().
        """
        client = self._get_client()
        last_exc: Exception | None = None
        for attempt in range(MAX_BACKOFF_RETRIES + 1):
            try:
                return await client.chat.completions.create(**kwargs)
            except Exception as e:
                if not self._is_backoff_retryable(e):
                    raise
                # Auth errors (401/403) are handled by credential pool, not backoff
                status = getattr(e, "status_code", None)
                if status in (401, 403):
                    raise
                last_exc = e
                if attempt < MAX_BACKOFF_RETRIES:
                    delay = (BACKOFF_BASE**attempt) * (1 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER))
                    await asyncio.sleep(max(0, delay))
                else:
                    raise
        raise last_exc  # unreachable

    async def close(self) -> None:
        """Close the underlying AsyncOpenAI client, releasing connection pools."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Check if an exception is an auth/rate-limit error worth retrying."""
        try:
            from openai import APIStatusError
        except ImportError:
            return False
        if isinstance(exc, APIStatusError):
            status = exc.status_code
            return status == 401 or status == 403 or status == 429
        # Also check by attribute for proxy/gateway errors without the SDK
        status_code = getattr(exc, "status_code", None)
        return status_code in (401, 403, 429)

    @staticmethod
    def _is_backoff_retryable(exc: Exception) -> bool:
        """Check if an exception should trigger jittered backoff retry.

        Covers: 429 (rate limit), timeout, connection error, 5xx.
        Does NOT cover: 401/403 (auth — handled by credential pool),
        400 (bad request — retrying won't help).
        """
        try:
            from openai import (
                APIConnectionError,
                APIStatusError,
                APITimeoutError,
                InternalServerError,
                RateLimitError,
            )
        except ImportError:
            return False
        if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
            return True
        if isinstance(exc, InternalServerError):
            return True
        if isinstance(exc, APIStatusError):
            return exc.status_code >= 500
        # Fallback: check by status_code attribute
        status_code = getattr(exc, "status_code", None)
        return status_code is not None and status_code >= 500

    def _on_auth_error(self) -> bool:
        """Handle auth/rate-limit error. Returns True if retry possible.

        Note: ``self.config`` is a mutable instance attribute on
        ``OpenAIChatClient`` (set in ``__init__``), NOT the frozen
        ``LLMConfig`` field.  Reassignment is safe — the ``frozen=True``
        on ``LLMConfig`` only forbids mutating fields *inside* a config
        instance, not rebinding the ``self.config`` attribute itself.
        """
        if self.pool is None:
            return False
        self.pool.mark_failed()
        self.config = self.pool.current
        self._client = None  # force re-creation with new key
        return True

    def for_model(self, model: str) -> OpenAIChatClient:
        return OpenAIChatClient(replace(self.config, model=model), pool=self.pool)

    async def stream(
        self,
        *,
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
        if self.config.reasoning_effort:
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        if self.config.service_tier:
            kwargs["service_tier"] = self.config.service_tier

        try:
            stream = await self._create_with_backoff(kwargs)
        except Exception as e:
            # Only rotate credentials on auth / rate-limit errors
            if self._is_retryable(e) and self._on_auth_error():
                # Update kwargs: new credential may have a different model
                kwargs["model"] = self.config.model
                client = self._get_client()
                stream = await client.chat.completions.create(**kwargs)
            else:
                raise

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

"""FakeLLMClient for unit testing — implements LLMClient Protocol.

Yields pre-programmed StreamEvent sequences without any network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from microagent.core.types import Message, TextDelta, ToolCallDelta, Usage
from microagent.llm.client import LLMClient, LLMConfig, StreamEvent, StreamDone


@dataclass
class ScriptedResponse:
    """One 'LLM response' consisting of stream events to replay."""
    events: list  # list of StreamEvent or callable


class FakeLLMClient:
    """Replays scripted responses in order, looping the last one."""

    def __init__(
        self,
        responses: list[ScriptedResponse],
        config: LLMConfig | None = None,
    ):
        self.config = config or LLMConfig("fake", "fake-key", "fake-model")
        self._responses = list(responses)
        self._call_index = 0
        self.calls: list[dict] = []  # record of all calls made

    def for_model(self, model: str) -> "FakeLLMClient":
        return FakeLLMClient(self._responses, config=LLMConfig(
            self.config.base_url, self.config.api_key, model
        ))

    async def stream(
        self, *,
        system: str,
        messages: tuple[Message, ...],
        tools: list | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({
            "system": system,
            "messages": list(messages),
            "tools": tools,
        })

        if not self._responses:
            yield Usage()
            yield StreamDone(usage=Usage(), stop_reason="stop")
            return

        idx = min(self._call_index, len(self._responses) - 1)
        self._call_index += 1
        response = self._responses[idx]

        for event in response.events:
            if callable(event):
                event_result = event(messages)
                if isinstance(event_result, list):
                    for e in event_result:
                        yield e
                else:
                    yield event_result
            else:
                yield event


# --- Helpers for building scripted responses ---

def text_response(text: str) -> ScriptedResponse:
    """A simple text response (no tool calls)."""
    return ScriptedResponse(events=[
        TextDelta(text=text),
        Usage(input_tokens=10, output_tokens=5),
        StreamDone(usage=Usage(input_tokens=10, output_tokens=5), stop_reason="stop"),
    ])


def tool_response(tool_calls: list[tuple[str, str, dict]]) -> ScriptedResponse:
    """A response that requests tool calls.

    Args: list of (id, name, arguments) tuples.
    """
    return ScriptedResponse(events=[
        ToolCallDelta(id=tid, name=name, arguments=args)
        for tid, name, args in tool_calls
    ] + [
        Usage(input_tokens=10, output_tokens=5),
        StreamDone(usage=Usage(input_tokens=10, output_tokens=5), stop_reason="tool_calls"),
    ])


def multi_turn(
    first: ScriptedResponse,
    *rest: ScriptedResponse,
) -> list[ScriptedResponse]:
    """Build a sequence of responses for a multi-iteration turn."""
    return [first, *rest]

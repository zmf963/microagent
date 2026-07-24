"""Tests for Overflow auto-recovery in SessionRunner.

When LLM returns stop_reason="length" and no content was streamed,
the runner should compact the conversation and retry once.
If retry also overflows, yield TurnFailed.
"""

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import (
    Message,
    TextDelta,
    TurnComplete,
    TurnFailed,
    Usage,
)
from microagent.llm.client import StreamDone
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from tests.unit.fake_llm import (
    FakeLLMClient,
    ScriptedResponse,
    text_response,
)


class TestOverflowRecovery:
    async def test_overflow_no_content_triggers_compact_retry(self):
        """stop_reason='length' with no streamed content → compact + retry."""
        # First response: overflow with no content
        overflow_resp = ScriptedResponse(
            events=[
                Usage(input_tokens=100, output_tokens=0),
                StreamDone(
                    usage=Usage(input_tokens=100, output_tokens=0),
                    stop_reason="length",
                ),
            ]
        )
        # Second response (after compact): success
        success_resp = text_response("recovered after compaction")

        llm = FakeLLMClient([overflow_resp, success_resp])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([]),
            budget=Budget(max_iterations=10),
            compression_threshold=100,
        )
        messages = [Message.user("hello")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)

        # Should have recovered, not failed
        completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(completes) >= 1
        assert "recovered" in completes[0].content

    async def test_overflow_with_content_does_not_retry(self):
        """stop_reason='length' but content was already streamed → fail."""
        overflow_resp = ScriptedResponse(
            events=[
                TextDelta(text="partial response"),
                Usage(input_tokens=100, output_tokens=5),
                StreamDone(
                    usage=Usage(input_tokens=100, output_tokens=5),
                    stop_reason="length",
                ),
            ]
        )
        llm = FakeLLMClient([overflow_resp])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([]),
            budget=Budget(max_iterations=10),
            compression_threshold=100,
        )
        messages = [Message.user("hello")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)

        # Should fail, not retry (content already streamed to user)
        fails = [e for e in events if isinstance(e, TurnFailed)]
        assert len(fails) >= 1
        assert "truncated" in fails[0].reason.lower() or "overflow" in fails[0].reason.lower()

    async def test_overflow_retry_still_overflows_fails(self):
        """If compact+retry also overflows → TurnFailed."""
        overflow1 = ScriptedResponse(
            events=[
                Usage(input_tokens=100, output_tokens=0),
                StreamDone(
                    usage=Usage(input_tokens=100, output_tokens=0),
                    stop_reason="length",
                ),
            ]
        )
        overflow2 = ScriptedResponse(
            events=[
                Usage(input_tokens=100, output_tokens=0),
                StreamDone(
                    usage=Usage(input_tokens=100, output_tokens=0),
                    stop_reason="length",
                ),
            ]
        )
        llm = FakeLLMClient([overflow1, overflow2])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([]),
            budget=Budget(max_iterations=10),
            compression_threshold=100,
        )
        messages = [Message.user("hello")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)

        fails = [e for e in events if isinstance(e, TurnFailed)]
        assert len(fails) >= 1

    async def test_overflow_recovery_consumes_budget(self):
        """Overflow recovery should consume budget for both attempts."""
        overflow_resp = ScriptedResponse(
            events=[
                Usage(input_tokens=50, output_tokens=0),
                StreamDone(
                    usage=Usage(input_tokens=50, output_tokens=0),
                    stop_reason="length",
                ),
            ]
        )
        success_resp = text_response("ok")

        llm = FakeLLMClient([overflow_resp, success_resp])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([]),
            budget=Budget(max_iterations=10),
            compression_threshold=100,
        )
        messages = [Message.user("hello")]
        async for event in runner.run_turn(messages):
            pass

        # Budget should have been consumed for the overflow attempt
        assert runner.budget._used_iter >= 1

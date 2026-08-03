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

    async def test_thinking_only_with_length_triggers_retry(self):
        """Thinking deltas alone must not count as content.

        A reasoning model that streams only thinking and then hits max
        tokens has produced NO user-visible content — the runner should
        treat it as an overflow and compact+retry, not fail with
        'truncated'.
        """
        overflow_resp = ScriptedResponse(
            events=[
                TextDelta(text="<internal reasoning>", kind="thinking"),
                Usage(input_tokens=100, output_tokens=50),
                StreamDone(
                    usage=Usage(input_tokens=100, output_tokens=50),
                    stop_reason="length",
                ),
            ]
        )
        success_resp = text_response("recovered")

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

        completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(completes) >= 1
        assert "recovered" in completes[0].content

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


class TestThinkingContentSplit:
    async def test_thinking_not_persisted_in_assistant_content(self):
        """kind='thinking' deltas are yielded to consumers but must NOT
        be persisted into the assistant message content."""
        from microagent.core.store import InMemoryStore

        resp = ScriptedResponse(
            events=[
                TextDelta(text="<secret chain of thought>", kind="thinking"),
                TextDelta(text="visible answer"),
                Usage(input_tokens=10, output_tokens=5),
                StreamDone(
                    usage=Usage(input_tokens=10, output_tokens=5),
                    stop_reason="end_turn",
                ),
            ]
        )
        store = InMemoryStore()
        llm = FakeLLMClient([resp])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([]),
            budget=Budget(max_iterations=5),
            store=store,
        )
        messages = [Message.user("hi")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)

        # Both deltas are still yielded to consumers (CLI renders thinking)
        texts = [e for e in events if isinstance(e, TextDelta)]
        assert any(t.kind == "thinking" for t in texts)

        # Persisted content contains ONLY the visible content
        history = await store.load_history(runner.session_id)
        assistant_msgs = [m for m in history if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0].content == "visible answer"
        # TurnComplete also reports only visible content
        completes = [e for e in events if isinstance(e, TurnComplete)]
        assert completes[0].content == "visible answer"

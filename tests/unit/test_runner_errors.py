"""Tests for error/edge paths in SessionRunner."""

import pytest
from microagent.core.types import Message, TurnFailed, TurnComplete, TextDelta, Usage
from microagent.llm.client import StreamDone
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.session.runner import SessionRunner
from microagent.session.budget import Budget
from tests.unit.fake_llm import FakeLLMClient, ScriptedResponse, text_response, tool_response


class TestRunnerErrorPaths:
    async def test_llm_returns_empty(self):
        """LLM returns no text and no tool calls."""
        llm = FakeLLMClient([text_response("")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]))
        messages = [Message.user("hi")]
        result = None
        async for event in runner.run_turn(messages):
            result = event
        assert isinstance(result, TurnComplete)
        assert result.content == ""

    async def test_llm_stream_length_truncation(self):
        """LLM response truncated — StreamDone stop_reason='length'."""
        resp = ScriptedResponse(events=[
            TextDelta(text="partial"),
            Usage(input_tokens=10, output_tokens=5),
            StreamDone(usage=Usage(input_tokens=10, output_tokens=5), stop_reason="length"),
        ])
        llm = FakeLLMClient([resp])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]))
        messages = [Message.user("hi")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        assert any(isinstance(e, TurnFailed) for e in events)

    async def test_tool_execution_continues_after_error(self):
        """Tool execution throws — error captured, loop continues."""
        llm = FakeLLMClient([
            tool_response([("c1", "bash", {"command": "nonexistent_binary_xyz"})]),
            text_response("tool failed but continuing"),
        ])
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(llm=llm, registry=reg, budget=Budget(max_iterations=5))
        messages = [Message.user("run bad command")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        # Should complete despite tool error
        completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(completes) >= 1

    async def test_budget_iteration_exhaustion(self):
        """Budget exhausted after max_iterations — TurnFailed."""
        llm = FakeLLMClient([
            tool_response([("c1", "bash", {"command": "echo 1"})]),
            tool_response([("c2", "bash", {"command": "echo 2"})]),
            tool_response([("c3", "bash", {"command": "echo 3"})]),
        ])
        reg = ToolRegistry(_default_builtins())
        budget = Budget(max_iterations=2)
        runner = SessionRunner(llm=llm, registry=reg, budget=budget)
        messages = [Message.user("do stuff")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        assert any(isinstance(e, TurnFailed) for e in events)
        assert "budget" in events[-1].reason.lower()

    async def test_compaction_guard_against_empty(self):
        """Compression should not crash on very short conversations."""
        from microagent.session.compress import compact_conversation, count_tokens
        messages = (Message.user("hi"), Message.assistant("hello"))
        tokens = count_tokens(messages)
        # Compaction with safe window
        result = await compact_conversation(
            messages, llm=None,
            context_window=10_000,
            force=False,
        )
        # Should return original (under threshold) — no crash
        assert len(result) == len(messages)

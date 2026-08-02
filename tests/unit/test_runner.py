"""Tests for SessionRunner — the core conversation loop."""

import pytest

from microagent.core.tool import ToolRegistry
from microagent.core.types import Message, ToolResult, TurnComplete, TurnFailed
from microagent.session.budget import Budget, BudgetExceeded
from microagent.session.runner import SessionRunner

from .fake_llm import FakeLLMClient, text_response, tool_response


class TestSessionRunnerSimple:
    """Tests where the LLM returns a text response immediately."""

    async def test_simple_text_response(self):
        llm = FakeLLMClient([text_response("Hello, world!")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry())
        messages = [Message.user("hi")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        assert any(isinstance(e, TurnComplete) and e.content == "Hello, world!" for e in events)
        # Messages should contain: user + assistant
        assert len(messages) == 2
        assert messages[1].role == "assistant"

    async def test_streaming_text_deltas(self):
        llm = FakeLLMClient([text_response("Hello, world!")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry())
        messages = [Message.user("hi")]
        deltas = []
        async for event in runner.run_turn(messages):
            from microagent.core.types import TextDelta

            if isinstance(event, TextDelta):
                deltas.append(event.text)
        assert "".join(deltas) == "Hello, world!"


class TestSessionRunnerWithTools:
    """Tests where the LLM requests tool calls."""

    async def test_tool_call_then_text(self):
        # LLM first calls read_file, then returns text
        from typing import Annotated

        from pydantic import Field

        from microagent.core.tool import tool

        @tool("echo_tool", description="Echoes back the input")
        async def echo_tool(
            msg: Annotated[str, Field(description="Message to echo")],
        ) -> ToolResult:
            return ToolResult.ok(f"echoed: {msg}")

        registry = ToolRegistry([echo_tool])
        llm = FakeLLMClient(
            [
                tool_response([("call_1", "echo_tool", {"msg": "hello"})]),
                text_response("I echoed your message."),
            ]
        )

        runner = SessionRunner(llm=llm, registry=registry)
        messages = [Message.user("echo hello")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)

        assert any(isinstance(e, TurnComplete) for e in events)
        # Messages: user + assistant(tool_call) + tool_result + assistant(text)
        assert len(messages) == 4
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert len(messages[1].tool_calls) == 1
        assert messages[2].role == "tool"
        assert messages[2].content == "echoed: hello"
        assert messages[3].role == "assistant"


class TestSessionRunnerBudgetExhaustion:
    async def test_budget_exhaustion(self):
        # LLM always returns tool calls → never finishes → budget exhausted
        llm = FakeLLMClient(
            [
                tool_response([("c", "unknown_tool", {})]),
            ]
        )
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry(),
            budget=Budget(max_iterations=2, max_tokens=999_999),
        )
        messages = [Message.user("loop forever")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        assert any(isinstance(e, TurnFailed) and "budget" in e.reason for e in events)

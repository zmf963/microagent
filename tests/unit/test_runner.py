"""Tests for SessionRunner — the core conversation loop."""

import pytest
from microagent.core.types import Message, ToolCall, ToolResult, TurnComplete, TurnFailed
from microagent.core.tool import ToolRegistry
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner

from .fake_llm import FakeLLMClient, text_response, tool_response


class TestBudget:
    def test_not_exhausted_initially(self):
        b = Budget(max_iterations=5)
        assert not b.exhausted
        assert b.remaining == 5

    def test_consume(self):
        b = Budget(max_iterations=3)
        b.consume(iterations=1)
        b.consume(iterations=1)
        assert b.remaining == 1
        assert not b.exhausted
        b.consume(iterations=1)
        assert b.exhausted

    def test_token_budget(self):
        b = Budget(max_tokens=100)
        b.consume(tokens=60)
        assert not b.exhausted
        b.consume(tokens=50)
        assert b.exhausted

    def test_cost_budget(self):
        b = Budget(max_cost_usd=1.0)
        b.consume(cost_usd=0.5)
        assert not b.exhausted
        b.consume(cost_usd=0.6)
        assert b.exhausted

    def test_summary(self):
        b = Budget(max_iterations=10, max_tokens=1000, max_cost_usd=1.0)
        b.consume(iterations=3, tokens=200, cost_usd=0.3)
        s = b.summary()
        assert "iterations=3/10" in s
        assert "tokens=200/1000" in s
        assert "0.3000" in s

    def test_reset(self):
        b = Budget(max_iterations=3)
        b.consume(iterations=3)
        assert b.exhausted
        b.reset()
        assert not b.exhausted


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
            msg: Annotated[str, Field(description="Message to echo")]
        ) -> ToolResult:
            return ToolResult.ok(f"echoed: {msg}")

        registry = ToolRegistry([echo_tool])
        llm = FakeLLMClient([
            tool_response([("call_1", "echo_tool", {"msg": "hello"})]),
            text_response("I echoed your message."),
        ])

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
        llm = FakeLLMClient([
            tool_response([("c", "unknown_tool", {})]),
        ])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry(),
            budget=Budget(max_iterations=2),
        )
        messages = [Message.user("loop forever")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        assert any(isinstance(e, TurnFailed) and "budget" in e.reason for e in events)

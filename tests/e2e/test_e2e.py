"""End-to-end tests — drive full agent turns through the real loop.

Marks: e2e. These use FakeLLMClient to script multi-step turns (tool call →
result → follow-up text), verifying the whole runner → tool → store →
memory pipeline works together, not just isolated units.
"""

import pytest

from microagent import Agent
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import (
    Message,
    TextDelta,
    ToolCallDelta,
    ToolResultDelta,
    TurnComplete,
    TurnFailed,
    Usage,
)
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from tests.unit.fake_llm import (
    FakeLLMClient,
    text_response,
    tool_response,
)


@pytest.mark.e2e
class TestFullAgentTurn:
    async def test_text_only_turn(self):
        """A plain text turn flows: user → TextDelta → TurnComplete."""
        fake = FakeLLMClient([text_response("Hello!")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        events = [e async for e in runner.run_turn([Message.user("hi")])]
        assert any(isinstance(e, TextDelta) for e in events)
        assert any(isinstance(e, TurnComplete) for e in events)
        await runner.close()

    async def test_tool_call_turn(self):
        """A tool-call turn flows: ToolCallDelta → execute → ToolResultDelta → final."""
        fake = FakeLLMClient([
            tool_response([("c1", "bash", {"command": "echo hi"})]),
            text_response("result is hi"),
        ])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        events = [e async for e in runner.run_turn([Message.user("run echo hi")])]
        types = [type(e).__name__ for e in events]
        assert "ToolCallDelta" in types
        assert "ToolResultDelta" in types
        assert "TurnComplete" in types
        # Order: tool call before tool result
        assert types.index("ToolCallDelta") < types.index("ToolResultDelta")
        await runner.close()

    async def test_multi_tool_parallel_turn(self):
        """Multiple tool calls in one turn execute concurrently."""
        fake = FakeLLMClient([
            tool_response([
                ("c1", "bash", {"command": "echo one"}),
                ("c2", "glob", {"pattern": "*.py"}),
            ]),
            text_response("both done"),
        ])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        events = [e async for e in runner.run_turn([Message.user("do two things")])]
        results = [e for e in events if isinstance(e, ToolResultDelta)]
        assert len(results) == 2
        assert any(isinstance(e, TurnComplete) for e in events)
        await runner.close()

    async def test_budget_exhaustion_yields_turnfailed(self):
        fake = FakeLLMClient([text_response("a"), text_response("b")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(max_iterations=1),
        )
        events = [e async for e in runner.run_turn([Message.user("hi")])]
        assert any(isinstance(e, TurnFailed) for e in events)
        await runner.close()

    async def test_usage_propagated(self):
        fake = FakeLLMClient([text_response("hi")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        events = [e async for e in runner.run_turn([Message.user("hi")])]
        assert any(isinstance(e, Usage) for e in events)
        await runner.close()


@pytest.mark.e2e
class TestFullAgentWithStore:
    async def test_turn_persists_to_store(self):
        from microagent.core.store import InMemoryStore
        store = InMemoryStore()
        fake = FakeLLMClient([text_response("stored reply")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(), store=store, session_id="s1",
        )
        msgs = [Message.user("remember this")]
        async for _ in runner.run_turn(msgs):
            pass
        history = await store.load_history("s1")
        assert len(history) >= 2  # user + assistant persisted
        await runner.close()

    async def test_resume_and_continue(self):
        from microagent.core.store import InMemoryStore
        store = InMemoryStore()
        await store.append("s1", Message.user("first q"))
        await store.append("s1", Message.assistant("first a"))
        fake = FakeLLMClient([text_response("second a")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(), store=store, session_id="s1",
        )
        history = await runner.resume("s1", store)
        msgs = list(history) + [Message.user("second q")]
        events = [e async for e in runner.run_turn(msgs)]
        assert any(isinstance(e, TurnComplete) for e in events)
        await runner.close()


@pytest.mark.e2e
class TestAgentFacade:
    async def test_arun_text(self):
        from microagent.llm.client import LLMConfig
        fake = FakeLLMClient([text_response("result")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        agent = Agent(runner=runner, registry=runner.registry)
        result = await agent.arun([Message.user("hi")])
        assert "result" in result
        await agent.close()

    async def test_agent_close_is_idempotent(self):
        from microagent.llm.client import LLMConfig
        fake = FakeLLMClient([text_response("ok")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        agent = Agent(runner=runner, registry=runner.registry)
        await agent.close()
        await agent.close()  # must not raise

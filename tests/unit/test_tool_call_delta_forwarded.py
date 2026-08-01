"""Behavioral regression: runner.run_turn must forward ToolCallDelta events.

The runner consumes the ToolCallDelta that llm.stream() yields (to build
its internal tool_calls list), but for a long time it never re-yielded
the event. The CLI's 🔧 tool-call panel handler was therefore dead code:
users saw the ✓ result panel with no preceding cyan panel showing the
tool name + arguments. This is the same class of bug as the historical
"Usage event swallowed" issue (commit 6c24b81).

This test drives the REAL SessionRunner with FakeLLMClient and asserts
the event stream actually contains a ToolCallDelta — the assertion that
was missing from test_review_fixes.py.
"""

import pytest

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, ToolCallDelta, TurnComplete
from microagent.session.runner import SessionRunner
from microagent.session.budget import Budget
from microagent.llm.client import StreamDone
from microagent.core.types import Usage
from tests.unit.fake_llm import FakeLLMClient, ScriptedResponse, text_response, tool_response


@pytest.mark.asyncio
async def test_run_turn_yields_tool_call_delta():
    """A tool-call turn must surface a ToolCallDelta in the event stream
    so the CLI can render the 🔧 panel before tool execution."""
    fake = FakeLLMClient([
        tool_response([("c1", "bash", {"command": "echo hi"})]),
        text_response("done"),
    ])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
    )
    events = []
    async for ev in runner.run_turn([Message.user("run echo")]):
        events.append(ev)

    tc_events = [e for e in events if isinstance(e, ToolCallDelta)]
    assert len(tc_events) >= 1, (
        "run_turn must yield ToolCallDelta so the CLI 🔧 tool-call panel "
        "can render before execution. Events seen: "
        f"{[type(e).__name__ for e in events]}"
    )
    tc = tc_events[0]
    assert tc.id == "c1"
    assert tc.name == "bash"
    assert tc.arguments == {"command": "echo hi"}
    await runner.close()


@pytest.mark.asyncio
async def test_tool_call_delta_precedes_tool_result():
    """In the event sequence, ToolCallDelta must come before any
    ToolResultDelta (the 🔧 panel renders before the ✓ panel)."""
    from microagent.core.types import ToolResultDelta

    fake = FakeLLMClient([
        tool_response([("c1", "bash", {"command": "echo hi"})]),
        text_response("done"),
    ])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
    )
    events = []
    async for ev in runner.run_turn([Message.user("run echo")]):
        events.append(ev)

    types = [type(e).__name__ for e in events]
    assert "ToolCallDelta" in types, f"missing ToolCallDelta: {types}"
    assert "ToolResultDelta" in types, f"missing ToolResultDelta: {types}"
    assert types.index("ToolCallDelta") < types.index("ToolResultDelta"), (
        f"ToolCallDelta must precede ToolResultDelta: {types}"
    )
    await runner.close()


@pytest.mark.asyncio
async def test_text_delta_still_yielded():
    """Guard against regression: TextDelta must still be forwarded too."""
    from microagent.core.types import TextDelta

    fake = FakeLLMClient([text_response("hello")])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
    )
    events = []
    async for ev in runner.run_turn([Message.user("hi")]):
        events.append(ev)
    assert any(isinstance(e, TextDelta) for e in events)
    assert any(isinstance(e, TurnComplete) for e in events)
    await runner.close()

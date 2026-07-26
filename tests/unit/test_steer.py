"""Tests for steer interrupt channel.

Agent.steer(text) injects text into the running turn by appending it
to the most recent tool_result. Steer arriving during a pure text
response waits until the next user input.
"""

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import (
    Message,
    TextDelta,
    ToolResultDelta,
    TurnComplete,
    TurnFailed,
)
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from microagent.core.types import SteerEvent
from tests.unit.fake_llm import (
    FakeLLMClient,
    ScriptedResponse,
    text_response,
    tool_response,
)


class TestSteer:
    async def test_steer_injected_into_tool_result(self):
        """Steer text appears in the next tool_result message."""
        # First response: tool call
        # Second response: text (after steer is injected)
        llm = FakeLLMClient(
            [
                tool_response([("c1", "bash", {"command": "echo hello"})]),
                text_response("done after steer"),
            ]
        )
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(llm=llm, registry=reg, budget=Budget(max_iterations=10))
        messages = [Message.user("run echo")]

        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
            # After tool result, inject steer before next LLM call
            if isinstance(event, ToolResultDelta):
                await runner.steer("STOP — do something else")

        # Check that steer was injected — the second LLM call's messages
        # should contain the steer text in a tool_result
        assert len(llm.calls) == 2
        second_call_messages = llm.calls[1]["messages"]
        # Find tool messages in second call
        tool_msgs = [m for m in second_call_messages if m.role == "tool"]
        assert any("STOP" in m.content for m in tool_msgs)

        # Should complete successfully
        completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(completes) >= 1

    async def test_steer_event_emitted(self):
        """SteerEvent is emitted when steer is injected."""
        llm = FakeLLMClient(
            [
                tool_response([("c1", "bash", {"command": "echo hi"})]),
                text_response("ok"),
            ]
        )
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(llm=llm, registry=reg, budget=Budget(max_iterations=10))
        messages = [Message.user("run echo")]

        steer_events = []
        async for event in runner.run_turn(messages):
            if isinstance(event, SteerEvent):
                steer_events.append(event)
            if isinstance(event, ToolResultDelta):
                await runner.steer("change direction")

        assert len(steer_events) >= 1
        assert "change direction" in steer_events[0].text

    async def test_steer_sets_pending_flag(self):
        """Agent.steer() sets _steer_pending on the runner."""
        llm = FakeLLMClient([text_response("hi")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]))
        assert runner._steer_pending is None
        await runner.steer("new instruction")
        assert runner._steer_pending == "new instruction"

    def test_steer_event_is_frozen_dataclass(self):
        """SteerEvent is a frozen dataclass with text field."""
        ev = SteerEvent(text="steer text")
        assert ev.text == "steer text"
        # Frozen
        try:
            ev.text = "other"
            assert False, "Should be frozen"
        except AttributeError:
            pass

    async def test_steer_pure_text_response_waits(self):
        """Steer during pure text response doesn't crash — waits next turn."""
        llm = FakeLLMClient([text_response("quick response")])
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(llm=llm, registry=reg, budget=Budget(max_iterations=10))
        messages = [Message.user("say hi")]

        # Steer during streaming (but FakeLLM is instant, so steer arrives after)
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)

        # Steer after turn completes — should just set pending, no crash
        await runner.steer("next instruction")
        assert runner._steer_pending == "next instruction"

        # The pending steer would be consumed on the next tool_result
        # but since turn is done, it stays pending
        completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(completes) >= 1

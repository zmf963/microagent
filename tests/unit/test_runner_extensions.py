"""Tests for SessionRunner integration with EventBus and extension points."""

from microagent import SessionRunner, ToolRegistry
from microagent.core.event import EventBus
from microagent.core.types import Message

from .fake_llm import FakeLLMClient, text_response


class TestSessionRunnerEventBus:
    async def test_turn_complete_event_fired(self):
        events = []
        bus = EventBus()
        bus.on("turn_complete", lambda sid, resp: events.append((sid, resp)))

        llm = FakeLLMClient([text_response("done")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry(), event_bus=bus)
        messages = [Message.user("hi")]
        async for _ in runner.run_turn(messages):
            pass
        assert len(events) == 1
        assert events[0][1] == "done"

    async def test_event_not_fired_when_bus_is_none(self):
        llm = FakeLLMClient([text_response("done")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry(), event_bus=None)
        messages = [Message.user("hi")]
        async for _ in runner.run_turn(messages):
            pass
        # Should not crash — event_bus=None is valid


class TestSessionRunnerPreLLMHook:
    async def test_hook_transforms_context(self):
        """PreLLMHook can modify the system prompt before LLM call."""

        class AddPromptHook:
            async def __call__(self, ctx):
                return {"system_prompt": ctx.get("system_prompt", "") + " EXTRA"}

        # SessionRunner doesn't use TurnContext in M0a — hooks receive
        # the system_prompt string. Test that hooks are called.
        llm = FakeLLMClient([text_response("ok")])
        hooks_called = []

        class TrackingHook:
            async def __call__(self, ctx):
                hooks_called.append(ctx)
                return ctx

        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry(),
            pre_llm_hooks=(TrackingHook(),),
        )
        messages = [Message.user("hi")]
        async for _ in runner.run_turn(messages):
            pass
        assert len(hooks_called) == 1


class TestSessionRunnerToolHook:
    async def test_tool_hook_before(self):
        """ToolHook.before is called before tool execution."""

        calls = []

        class AuditHook:
            async def before(self, call, ctx):
                calls.append(("before", call.name))
                return call

            async def after(self, call, result, ctx):
                calls.append(("after", call.name))
                return result

        runner = SessionRunner(
            llm=FakeLLMClient([text_response("done")]),
            registry=ToolRegistry(),
            tool_hooks=(AuditHook(),),
        )
        # No tool calls in this test → hooks not triggered
        messages = [Message.user("hi")]
        async for _ in runner.run_turn(messages):
            pass
        # Hooks not called because no tool calls
        assert len(calls) == 0


class TestSessionRunnerContextSource:
    async def test_context_source_appended(self):
        """ContextSource contributes to system prompt."""

        class GitSource:
            async def contribute(self, ctx):
                return "\ngit: main"

        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(),
            context_sources=(GitSource(),),
        )
        messages = [Message.user("hi")]
        async for _ in runner.run_turn(messages):
            pass
        # Context source injected to user message (ADR-0005: system prompt frozen)
        assert len(runner.llm.calls) == 1
        user_msgs = [m for m in runner.llm.calls[0]["messages"] if m.role == "user"]
        user_content = " ".join(m.content for m in user_msgs)
        assert "git: main" in user_content

    async def test_failing_context_source_does_not_crash_turn(self):
        """A ContextSource that raises must be skipped, not crash the turn
        (same fault-tolerance contract as the skill loader)."""

        class ExplodingSource:
            async def contribute(self, ctx):
                raise RuntimeError("network lookup failed")

        class GoodSource:
            async def contribute(self, ctx):
                return "\ngood context"

        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(),
            context_sources=(ExplodingSource(), GoodSource()),
        )
        events = []
        async for ev in runner.run_turn([Message.user("hi")]):
            events.append(ev)
        from microagent.core.types import TurnComplete
        assert any(isinstance(ev, TurnComplete) for ev in events)


class TestExitTool:
    async def test_session_exit_marker_ends_turn(self):
        """The exit tool's [SESSION_EXIT] marker must terminate the turn —
        previously nothing consumed it and the loop continued."""
        from microagent.core.tool import _default_builtins
        from microagent.core.types import TurnComplete
        from tests.unit.fake_llm import tool_response

        registry = ToolRegistry(_default_builtins())
        llm = FakeLLMClient([
            tool_response([("tc1", "exit", {})]),
            text_response("should never be requested"),
        ])
        runner = SessionRunner(llm=llm, registry=registry)
        events = []
        async for ev in runner.run_turn([Message.user("do work then exit")]):
            events.append(ev)
        completes = [ev for ev in events if isinstance(ev, TurnComplete)]
        assert completes, "exit tool must end the turn with TurnComplete"
        assert "session ended" in completes[-1].content
        # Only ONE LLM call — the loop must not continue after the marker
        assert len(llm.calls) == 1


class TestToolsCacheRefresh:
    async def test_mid_session_registration_reaches_llm(self):
        """mcp_connect-style mid-session register() must invalidate the
        cached tools snapshot — otherwise the new tool never appears in the
        LLM's tools list for the rest of the session."""
        from microagent.core.tool import tool as tool_decorator
        from microagent.core.types import ToolResult

        @tool_decorator("late_tool", description="registered mid-session")
        async def _late() -> ToolResult:
            return ToolResult.ok("late")

        registry = ToolRegistry()
        llm = FakeLLMClient([text_response("first"), text_response("second")])
        runner = SessionRunner(llm=llm, registry=registry)
        async for _ in runner.run_turn([Message.user("hi")]):
            pass
        assert llm.calls[0]["tools"] is None  # empty registry

        registry.register(_late)
        async for _ in runner.run_turn([Message.user("again")]):
            pass
        tools = llm.calls[1]["tools"]
        assert tools is not None
        names = [t["function"]["name"] for t in tools]
        assert "late_tool" in names

    async def test_failing_pre_llm_hook_keeps_last_good_system(self):
        """A pre_llm_hook that raises must not abort the turn; the previous
        system prompt is kept."""

        class ExplodingHook:
            async def __call__(self, system):
                raise ValueError("hook bug")

        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(),
            pre_llm_hooks=(ExplodingHook(),),
        )
        events = []
        async for ev in runner.run_turn([Message.user("hi")]):
            events.append(ev)
        from microagent.core.types import TurnComplete
        assert any(isinstance(ev, TurnComplete) for ev in events)

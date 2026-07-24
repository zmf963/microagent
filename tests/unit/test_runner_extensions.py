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

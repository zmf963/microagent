"""Tests for bug fixes and integration wiring from code review."""

import pytest
import tempfile
from pathlib import Path

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, ToolCall, TurnComplete, Usage
from microagent.core.permission import Rule, Decision, PermissionEngine, DEFAULT_RULES
from microagent.llm.client import LLMConfig
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from tests.unit.fake_llm import FakeLLMClient, text_response, tool_response, ScriptedResponse
from microagent.llm.client import StreamDone


class TestToolCacheMode:
    async def test_mode_change_invalidates_cache(self):
        """Switching mode should rebuild tool cache."""
        llm = FakeLLMClient([text_response("ok"), text_response("ok")])
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(llm=llm, registry=reg, budget=Budget(max_iterations=10))

        # First turn: normal mode — all tools
        async for _ in runner.run_turn([Message.user("hi")]):
            pass
        normal_tools = llm.calls[0].get("tools")
        # Should have write_file
        assert normal_tools is not None

        # Switch to plan mode
        runner.mode = "plan"
        async for _ in runner.run_turn([Message.user("test")]):
            pass
        plan_tools = llm.calls[1].get("tools")
        # Plan mode should have fewer tools (no write_file)
        assert plan_tools is not None
        tool_names = [t["function"]["name"] for t in plan_tools]
        assert "write_file" not in tool_names


class TestOverflowFix:
    async def test_overflow_no_false_trigger_on_tool_only(self):
        """stop_reason=length with tool calls but no text → should NOT retry."""
        from microagent.core.types import ToolCallDelta

        # Response with tool calls AND stop_reason=length — should execute tools, not retry
        resp = ScriptedResponse(
            events=[
                ToolCallDelta(id="tc1", name="bash", arguments={"command": "echo hi"}),
                Usage(input_tokens=100, output_tokens=5),
                StreamDone(usage=Usage(input_tokens=100, output_tokens=5), stop_reason="length"),
            ]
        )
        # Second response after tool execution
        resp2 = text_response("done")
        llm = FakeLLMClient([resp, resp2])
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(
            llm=llm, registry=reg, budget=Budget(max_iterations=10), compression_threshold=100,
        )
        messages = [Message.user("run command")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        # Should NOT have triggered overflow recovery — tool was executed
        # Should NOT see TurnFailed from overflow
        from microagent.core.types import TurnFailed as TF
        fails = [e for e in events if isinstance(e, TF) and "overflow" in str(e).lower()]
        assert len(fails) == 0


class TestASKRules:
    def test_bash_rm_patterns(self):
        """ASK rules should match rm/mv/chmod/chown commands."""
        rules = DEFAULT_RULES
        engine = PermissionEngine(rules=rules)

        # rm should ask
        call = ToolCall(id="c1", name="bash", arguments={"command": "rm -rf /"})
        decision = engine.resolve("bash")
        assert decision == Decision.ALLOW  # resolve returns most permissive

        # Check evaluate
        import asyncio
        result = asyncio.run(engine.evaluate(call))
        assert result.decision == Decision.ASK

    def test_bash_redirect_asks(self):
        """Output redirection should trigger ASK."""
        rules = DEFAULT_RULES
        engine = PermissionEngine(rules=rules)
        call = ToolCall(id="c1", name="bash", arguments={"command": "echo hi > /tmp/out"})
        import asyncio
        result = asyncio.run(engine.evaluate(call))
        assert result.decision == Decision.ASK

    def test_task_always_asks(self):
        """task tool should always ASK."""
        rules = DEFAULT_RULES
        engine = PermissionEngine(rules=rules)
        call = ToolCall(id="c1", name="task", arguments={"prompt": "test"})
        import asyncio
        result = asyncio.run(engine.evaluate(call))
        assert result.decision == Decision.ASK


class TestPlanBuildCommands:
    def test_commands_registered(self):
        """plan/build are in command registry."""
        from microagent.surface.cli import _COMMANDS
        assert "plan" in _COMMANDS
        assert "build" in _COMMANDS

    async def test_plan_mode_blocks_process(self):
        """Plan mode should also block process tool."""
        from microagent.session.runner import SessionRunner
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(llm=FakeLLMClient([]), registry=reg)
        runner.mode = "plan"
        available = runner._get_available_tools()
        assert "process" not in available
        assert "write_file" not in available


class TestConfigToolset:
    def test_config_has_toolset_field(self):
        """Config should have toolset field."""
        from microagent.config import Config
        config = Config(llm=LLMConfig("http://x", "k", "m"))
        assert config.toolset == "core,extended"
        assert hasattr(config, "toolset")

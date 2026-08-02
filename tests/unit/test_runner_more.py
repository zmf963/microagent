"""Tests for SessionRunner uncovered paths: memory extractor, close
cleanup, interrupt, plan-mode filtering, steer cascade, overflow recovery."""

import pytest

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import (
    Message,
    TurnComplete,
    TurnFailed,
    Usage,
    ToolCall,
)
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from microagent.llm.client import StreamDone
from tests.unit.fake_llm import FakeLLMClient, text_response, tool_response, ScriptedResponse


class TestMemoryAndClose:
    @pytest.mark.asyncio
    async def test_memory_extractor_created_and_closed(self):
        """A runner with memory config creates an extractor; close() cleans it."""
        from microagent.memory.provider import SQLiteMemoryProvider
        import tempfile
        mem = SQLiteMemoryProvider(tempfile.mktemp(suffix=".db"))
        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
            memory=mem,
        )
        assert runner._extractor is not None
        await runner.close()
        mem.close()

    @pytest.mark.asyncio
    async def test_close_with_browser_and_lsp_state(self):
        """close() tolerates browser page / LSP clients that error on close."""
        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
        )

        class _BadPage:
            async def close(self):
                raise RuntimeError("page close failed")

        class _BadLSP:
            async def shutdown(self):
                raise RuntimeError("lsp shutdown failed")

        runner._browser_state.page = _BadPage()
        runner._lsp_state.clients["python"] = _BadLSP()
        await runner.close()  # must not raise

    @pytest.mark.asyncio
    async def test_close_kills_background_procs(self):
        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
        )
        # Start a real background process via the process tool
        from microagent.tools.builtins import process as proc_mod
        proc_mod._current_registry.set(runner._proc_registry)
        sid = (await proc_mod.process.fn(action="start", command="sleep 30")).content.strip()
        assert runner._proc_registry.procs[sid].returncode is None
        await runner.close()
        assert sid not in runner._proc_registry.procs


class TestInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_mid_turn(self):
        """interrupt() set during a turn yields TurnFailed.

        Note: run_turn() resets _interrupt_requested at entry, so an
        interrupt must be set DURING the turn (e.g. from the event loop).
        """
        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
        )
        events = []

        async def _run():
            async for e in runner.run_turn([Message.user("hi")]):
                events.append(e)
                # Interrupt after the first event (mid-turn)
                runner.interrupt()

        await _run()
        assert any(isinstance(e, TurnFailed) for e in events), (
            f"expected TurnFailed after mid-turn interrupt, got {[type(e).__name__ for e in events]}"
        )
        await runner.close()


class TestPlanMode:
    @pytest.mark.asyncio
    async def test_plan_mode_blocks_write_tools(self):
        """In plan mode, write tools are filtered from the LLM's tool list."""
        fake = FakeLLMClient([text_response("plan")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
        )
        runner.mode = "plan"
        async for _ in runner.run_turn([Message.user("analyze")]):
            pass
        # The LLM's tool list must exclude write tools
        tools = fake.calls[0]["tools"] or []
        names = [t["function"]["name"] for t in tools]
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "read_file" in names  # read tools kept
        await runner.close()

    @pytest.mark.asyncio
    async def test_get_available_tools(self):
        runner = SessionRunner(
            llm=FakeLLMClient([]), registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
        )
        avail = runner._get_available_tools()
        assert "read_file" in avail
        assert "write_file" in avail  # build mode has all
        runner.mode = "plan"
        avail_plan = runner._get_available_tools()
        assert "write_file" not in avail_plan
        assert "browser_navigate" not in avail_plan
        await runner.close()


class TestOverflowRecovery:
    @pytest.mark.asyncio
    async def test_overflow_with_no_content_or_tools(self):
        """stop_reason=length with no content and no tools → overflow recovery."""
        from microagent.core.types import Usage as U
        resp = ScriptedResponse(events=[
            U(input_tokens=10, output_tokens=5),
            StreamDone(usage=U(input_tokens=10, output_tokens=5), stop_reason="length"),
        ])
        fake = FakeLLMClient([resp, text_response("recovered")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(max_iterations=5),
            compression_threshold=10**9,  # huge so auto-compress won't fire
        )
        events = [e async for e in runner.run_turn([Message.user("long prompt")])]
        # After overflow recovery, the second call should produce a TurnComplete
        assert any(isinstance(e, TurnComplete) for e in events), (
            f"expected TurnComplete after overflow recovery, got {[type(e).__name__ for e in events]}"
        )
        await runner.close()


class TestSteerCascade:
    @pytest.mark.asyncio
    async def test_steer_cascades_to_subagents(self):
        """steer() propagates to active subagents."""
        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
        )
        child_runner = SessionRunner(
            llm=FakeLLMClient([text_response("child ok")]),
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
        )
        runner._active_subagents.append(child_runner)
        await runner.steer("cascade me")
        assert child_runner._steer_pending == "cascade me"
        await runner.close()
        await child_runner.close()


class TestContextSources:
    @pytest.mark.asyncio
    async def test_context_sources_contribute(self):
        """Context sources inject content into the user message sent to the LLM."""
        class _Source:
            async def contribute(self, ctx):
                return "\n[source: project info]"

        fake = FakeLLMClient([text_response("ok")])
        runner = SessionRunner(
            llm=fake,
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(),
            context_sources=(_Source(),),
        )
        msgs = [Message.user("hi")]
        async for _ in runner.run_turn(msgs):
            pass
        # The LLM received the source content appended to the user message
        llm_msgs = fake.calls[0]["messages"]
        assert any("[source: project info]" in m.content for m in llm_msgs if m.role == "user")
        # Original messages list is unchanged (injection uses a copy)
        assert msgs[0].content == "hi"
        await runner.close()

"""Regression test: SessionRunner.close() must clean up background processes
and MCP connections.

Before the fix, close() cleaned up the browser page and LSP servers but
left every `process start` subprocess running and every mcp_connect()
child alive — orphan processes accumulated across sessions.
"""

import asyncio
import pytest

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.session.runner import SessionRunner
from microagent.session.budget import Budget
from tests.unit.fake_llm import FakeLLMClient, text_response


@pytest.mark.asyncio
async def test_close_kills_background_processes():
    """A process started via the `process` tool must be killed on close()."""
    fake = FakeLLMClient([text_response("ok")])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
    )

    # Simulate the runner setting up ContextVars the way _settle does.
    from microagent.tools.builtins import process as _proc_mod
    _proc_mod._current_registry.set(runner._proc_registry)

    # Start a long-running subprocess via the process tool.
    proc_result = await _proc_mod.process.fn(
        action="start", command="sleep 300",
    )
    assert not proc_result.is_error, proc_result.content
    sid = proc_result.content.strip()
    proc = runner._proc_registry.procs[sid]
    assert proc.returncode is None  # running

    await runner.close()

    # Process was killed and removed from the registry.
    assert proc.returncode is not None, "close() did not kill the subprocess"
    assert sid not in runner._proc_registry.procs


@pytest.mark.asyncio
async def test_close_disconnects_mcp_managers():
    """MCP managers tracked by mcp_connect must be disconnected on close()."""
    fake = FakeLLMClient([text_response("ok")])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
    )

    # Inject a fake manager that records disconnect() calls.
    disconnect_calls = []

    class FakeManager:
        async def disconnect(self):
            disconnect_calls.append(True)

    runner._mcp_managers["fake-server"] = FakeManager()

    await runner.close()

    assert len(disconnect_calls) == 1, "close() did not disconnect MCP managers"
    assert runner._mcp_managers == {}, "close() did not clear the manager dict"


@pytest.mark.asyncio
async def test_close_clears_proc_outputs():
    """Output buffers should not leak across sessions either."""
    fake = FakeLLMClient([text_response("ok")])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
    )
    # Simulate a completed process whose output buffer is still around.
    dead_proc = await asyncio.create_subprocess_shell("true")
    await dead_proc.wait()
    runner._proc_registry.procs["stale-sid"] = dead_proc
    runner._proc_registry.outputs["stale-sid"] = ["old output"]

    await runner.close()

    assert runner._proc_registry.outputs == {}
    assert runner._proc_registry.procs == {}

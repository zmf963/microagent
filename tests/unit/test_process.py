"""Tests for process management tool — background process tracking."""

import asyncio

import pytest

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


@pytest.fixture
def proc_tool():
    reg = ToolRegistry(_default_builtins())
    return reg.get("process")


class TestProcessManagement:
    async def test_tool_registered(self):
        """process tool is registered in the default builtins."""
        reg = ToolRegistry(_default_builtins())
        assert "process" in reg.names

    async def test_start_and_poll(self, proc_tool):
        """Start a process and poll its output."""
        r1 = await proc_tool.execute(
            ToolCall(
                id="p1",
                name="process",
                arguments={
                    "action": "start",
                    "command": "echo hello",
                },
            )
        )
        assert r1.is_error is False
        session_id = r1.content

        await asyncio.sleep(0.3)

        r2 = await proc_tool.execute(
            ToolCall(
                id="p2",
                name="process",
                arguments={
                    "action": "poll",
                    "session_id": session_id,
                },
            )
        )
        assert "hello" in r2.content.lower()

    async def test_kill(self, proc_tool):
        """Kill a running process."""
        r1 = await proc_tool.execute(
            ToolCall(
                id="p1",
                name="process",
                arguments={
                    "action": "start",
                    "command": "sleep 30",
                },
            )
        )
        session_id = r1.content

        r2 = await proc_tool.execute(
            ToolCall(
                id="p2",
                name="process",
                arguments={
                    "action": "kill",
                    "session_id": session_id,
                },
            )
        )
        assert "killed" in r2.content.lower() or "terminated" in r2.content.lower()

    async def test_list(self, proc_tool):
        """List running processes."""
        r = await proc_tool.execute(
            ToolCall(
                id="p1",
                name="process",
                arguments={
                    "action": "list",
                },
            )
        )
        assert r.is_error is False
        assert isinstance(r.content, str)

    async def test_invalid_action(self, proc_tool):
        """Invalid action returns error."""
        r = await proc_tool.execute(
            ToolCall(
                id="p1",
                name="process",
                arguments={
                    "action": "invalid",
                },
            )
        )
        assert r.is_error is True

    async def test_poll_nonexistent(self, proc_tool):
        """Polling a nonexistent session returns error."""
        r = await proc_tool.execute(
            ToolCall(
                id="p1",
                name="process",
                arguments={
                    "action": "poll",
                    "session_id": "nonexistent-123",
                },
            )
        )
        assert r.is_error is True or "not found" in r.content.lower()


class TestProcessSessionIsolation:
    """Verify that process registries are isolated per SessionRunner."""

    async def test_two_runners_have_separate_registries(self):
        """Processes started in one runner are invisible to another."""
        from microagent.session.budget import Budget
        from microagent.session.runner import SessionRunner

        from tests.unit.fake_llm import FakeLLMClient, text_response

        # Runner A — starts a long-running process
        runner_a = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(max_iterations=5),
        )
        # Runner B — separate runner, should NOT see A's processes
        runner_b = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(max_iterations=5),
        )

        proc = runner_a.registry.get("process")

        # Set runner A's context and start a process
        from microagent.tools.builtins import process as _proc_module

        _proc_module._current_registry.set(runner_a._proc_registry)
        r_start = await proc.execute(
            ToolCall(
                id="pa1",
                name="process",
                arguments={"action": "start", "command": "sleep 30"},
            )
        )
        assert r_start.is_error is False
        sid_a = r_start.content

        # Switch to runner B's context
        _proc_module._current_registry.set(runner_b._proc_registry)
        r_list_b = await proc.execute(
            ToolCall(
                id="pb1",
                name="process",
                arguments={"action": "list"},
            )
        )
        # Runner B should see NO processes
        assert "no processes" in r_list_b.content.lower()

        # Switch back to runner A — process should still be there
        _proc_module._current_registry.set(runner_a._proc_registry)
        r_list_a = await proc.execute(
            ToolCall(
                id="pa2",
                name="process",
                arguments={"action": "list"},
            )
        )
        assert sid_a in r_list_a.content

        # Cleanup
        await proc.execute(
            ToolCall(
                id="pa3",
                name="process",
                arguments={"action": "kill", "session_id": sid_a},
            )
        )

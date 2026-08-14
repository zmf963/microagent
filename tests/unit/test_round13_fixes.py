"""Round 13 regression tests — findings from the 13th review round.

Covers the probe-verified bugs:
1. mcp_connect idempotency lock was per-task (concurrent calls double-spawn)
2. budget exhaustion at consume_usage left orphaned tool_calls in the store
3. process write drain() hung forever on non-reading processes
4. runner.close() hung when a killed process had unread pipe data
5. mcp dead-manager reconnect unregisters stale adapters
6. LocalTerminal timeout/cancel hangs on pipe-full kill
7. browser_navigate rejects redirects to internal targets
8. plan mode blocks the git tool and mcp_connect
9. /skill unload actually filters runner injection
10. write_file backup has a size cap
11. attachments refuse system paths from untrusted text
"""

import asyncio

import pytest

from microagent.core.store import InMemoryStore, SQLiteStore
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, ToolCall, ToolCallDelta, ToolResult, Usage
from microagent.llm.client import StreamDone
from microagent.session.budget import Budget, BudgetExceeded
from microagent.session.runner import SessionRunner


# ---------------------------------------------------------------------------
# 1. mcp_connect concurrent double-spawn
# ---------------------------------------------------------------------------


class _CountingConnect:
    """Fake mcp client that counts invocations and simulates a slow server."""

    def __init__(self):
        self.count = 0

    async def __call__(self, command, registry):
        self.count += 1
        await asyncio.sleep(0.2)

        class _Mgr:
            _task = None  # None → treated as live → idempotent skip
            async def disconnect(self, registry=None):
                pass

        return _Mgr()


class _MCPConnectLLM:
    def __init__(self, calls: list[tuple[str, str, dict]]):
        self._calls = list(calls)
        self.config = type(
            "C", (), {"model": "test", "base_url": "", "api_key": "", "auxiliary_model": None}
        )()

    async def stream(self, system, messages, tools):
        for tid, name, args in self._calls:
            yield ToolCallDelta(id=tid, name=name, arguments=args)
        yield Usage()
        yield StreamDone(usage=Usage(), stop_reason="tool_calls")

    def for_model(self, m):
        return self


class TestMCPConnectIdempotencyLock:
    async def test_concurrent_same_server_connects_once(self, monkeypatch):
        """Two mcp_connect('git') calls in one turn spawn one subprocess."""
        from microagent.mcp import client as mcp_client

        fake = _CountingConnect()
        monkeypatch.setattr(mcp_client, "connect_mcp_stdio", fake)

        llm = _MCPConnectLLM(
            [
                ("c1", "mcp_connect", {"name": "git"}),
                ("c2", "mcp_connect", {"name": "git"}),
                ("c3", "mcp_connect", {"name": "sqlite"}),
            ]
        )
        runner = SessionRunner(
            llm=llm, registry=ToolRegistry(_default_builtins()), budget=Budget()
        )
        messages = [Message.user("connect git and sqlite")]
        async for _ in runner.run_turn(messages):
            pass
        # git connected once (idempotent skip), sqlite once → 2 total
        assert fake.count == 2


# ---------------------------------------------------------------------------
# 2. budget exhaustion → no orphaned tool_calls
# ---------------------------------------------------------------------------


class _UsageExplodingBudget(Budget):
    async def consume_usage(self, usage):
        raise BudgetExceeded("budget exhausted: cost limit reached")


class _ToolCallThenUsageLLM:
    def __init__(self):
        self.config = type(
            "C", (), {"model": "test", "base_url": "", "api_key": "", "auxiliary_model": None}
        )()

    async def stream(self, system, messages, tools):
        yield ToolCallDelta(id="c1", name="bash", arguments={"command": "echo hi"})
        yield Usage(input_tokens=10, output_tokens=10, cost_usd=1.0)

    def for_model(self, m):
        return self


class TestBudgetExhaustionNoOrphan:
    async def test_tool_calls_get_error_results_on_budget_exhaustion(self, tmp_path):
        """consume_usage BudgetExceeded must not leave orphaned tool_calls
        in the store — OpenAI rejects such sessions on resume."""
        store = SQLiteStore(tmp_path / "s.db")
        runner = SessionRunner(
            llm=_ToolCallThenUsageLLM(),
            registry=ToolRegistry([]),
            budget=_UsageExplodingBudget(),
            store=store,
            session_id="s1",
        )
        messages = [Message.user("hi")]
        events = []
        async for e in runner.run_turn(messages):
            events.append(e)
        assert any(getattr(e, "reason", "").startswith("budget") for e in events)

        hist = await store.load_history("s1")
        orphaned = [
            tc.id
            for m in hist
            for tc in (m.tool_calls or ())
            if not any(t.tool_call_id == tc.id for t in hist)
        ]
        assert orphaned == []
        # every tool_call got an error result
        tool_ids = {t.tool_call_id for t in hist if t.role == "tool"}
        call_ids = {tc.id for m in hist for tc in (m.tool_calls or ())}
        assert call_ids == tool_ids


# ---------------------------------------------------------------------------
# 3. process write drain timeout
# ---------------------------------------------------------------------------


class TestProcessWriteTimeout:
    async def test_write_to_non_reading_process_returns_error(self):
        """A 10MB write to a process that never reads stdin must not hang."""
        import os
        import signal

        from microagent.tools.builtins import process as proc_mod

        p = await asyncio.create_subprocess_shell(
            "sleep 100",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            reg = proc_mod.ProcRegistry()
            reg.procs["p1"] = p
            reg.outputs["p1"] = []
            reg.dropped["p1"] = 0
            proc_mod._current_registry.set(reg)

            registry = ToolRegistry(_default_builtins())
            tool_fn = registry.get("process")
            assert tool_fn is not None
            call = ToolCall(
                id="c1",
                name="process",
                arguments={
                    "action": "write",
                    "session_id": "p1",
                    "data": "x" * 10_000_000,
                },
            )
            result = await asyncio.wait_for(tool_fn.execute(call), timeout=10)
            assert result.is_error
            assert "not reading stdin" in result.content
        finally:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 4. runner.close() bounded wait
# ---------------------------------------------------------------------------


class TestRunnerCloseBounded:
    async def test_close_returns_with_unread_pipe_data(self):
        """A killed process with a full pipe must not hang close() forever."""
        import os
        import signal

        from microagent.tools.builtins import process as proc_mod

        llm = _MCPConnectLLM([])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget())
        p = await asyncio.create_subprocess_shell(
            "yes spamline",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await asyncio.sleep(0.8)  # fill the pipe buffer, yes blocks on write
        runner._proc_registry.procs["p1"] = p
        runner._proc_registry.outputs["p1"] = []
        try:
            await asyncio.wait_for(runner.close(), timeout=8)
        finally:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 5. mcp dead-manager reconnect unregisters stale adapters
# ---------------------------------------------------------------------------


class _DeadManager:
    def __init__(self, task):
        self._task = task  # done task → reconnect path
        self.disconnected_registry = None

    async def disconnect(self, registry=None):
        self.disconnected_registry = registry


class TestMCPReconnectUnregisters:
    async def test_dead_manager_disconnect_receives_registry(self):
        """mcp_connect must pass the registry to a dead manager's
        disconnect() so stale adapters get unregistered."""
        from microagent.tools.builtins import mcp_connect as mcp_mod
        from microagent.tools.builtins import task as task_mod

        done_task = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0.01)  # let it complete → done()
        dead = _DeadManager(done_task)
        managers = {"git": dead}
        mcp_mod._current_managers.set(managers)

        class _RunnerStub:
            class _Registry:
                names = ["stale_tool"]

            registry = _Registry()

        task_mod._current_runner.set(_RunnerStub())

        from microagent.mcp import client as mcp_client

        async def fake_connect(command, registry):
            class _Mgr:
                _task = asyncio.current_task()
                async def disconnect(self, registry=None):
                    pass

            return _Mgr()

        orig = mcp_client.connect_mcp_stdio
        mcp_client.connect_mcp_stdio = fake_connect
        try:
            registry = ToolRegistry(_default_builtins())
            tool_fn = registry.get("mcp_connect")
            assert tool_fn is not None
            call = ToolCall(id="c1", name="mcp_connect", arguments={"name": "git"})
            result = await tool_fn.execute(call)
            assert not result.is_error
            assert dead.disconnected_registry is _RunnerStub.registry
        finally:
            mcp_client.connect_mcp_stdio = orig


# ---------------------------------------------------------------------------
# 6. LocalTerminal bounded wait on kill
# ---------------------------------------------------------------------------


class TestLocalTerminalPipeFull:
    async def test_timeout_with_grandchild_returns(self):
        """timeout with a grandchild holding the pipe open must return."""
        from microagent.terminal.backend import LocalTerminal

        t = LocalTerminal()
        result = await asyncio.wait_for(
            t.run("sh -c 'sleep 300 & sleep 30'", timeout=0.5), timeout=10
        )
        assert result.timed_out
        assert result.exit_code == -1


# ---------------------------------------------------------------------------
# 7. browser redirect SSRF (unit-level: URL check helper reuse)
# ---------------------------------------------------------------------------


class TestBrowserRedirectCheck:
    def test_navigate_check_rejects_internal_target(self):
        from microagent.tools.builtins.browser import _check_navigate_url

        err = _check_navigate_url("http://169.254.169.254/latest/meta-data/")
        assert err is not None
        err = _check_navigate_url("http://192.168.1.1/admin")
        assert err is not None

    def test_navigate_check_allows_public(self):
        from microagent.tools.builtins.browser import _check_navigate_url

        assert _check_navigate_url("https://example.com/") is None


# ---------------------------------------------------------------------------
# 8. plan mode blocks git tool + mcp_connect
# ---------------------------------------------------------------------------


class TestPlanModeBlockedTools:
    def test_git_and_mcp_connect_are_blocked(self):
        runner = SessionRunner(llm=_MCPConnectLLM([]), registry=ToolRegistry([]))
        runner.mode = "plan"
        assert "git" in runner._PLAN_BLOCKED_TOOLS
        assert "mcp_connect" in runner._PLAN_BLOCKED_TOOLS
        assert "git" not in runner._get_available_tools()
        assert "mcp_connect" not in runner._get_available_tools()


# ---------------------------------------------------------------------------
# 9. disabled_skills filters runner injection
# ---------------------------------------------------------------------------


class _FakeSkill:
    def __init__(self, name, namespace="ns", description="d", body="BODY", triggers=()):
        self.name = name
        self.namespace = namespace
        self.description = description
        self.body = body
        self.triggers = triggers


class _FakeLoaded:
    def __init__(self, skill, reason="kw", score=1.0):
        self.skill = skill
        self.reason = reason
        self.score = score


class _FakeSkillLoader:
    def __init__(self, skills):
        self._skills = skills

    async def load(self):
        return tuple(self._skills)

    async def match(self, user_input):
        return tuple(_FakeLoaded(s) for s in self._skills)


class _EchoLLM:
    def __init__(self):
        self.config = type(
            "C", (), {"model": "test", "base_url": "", "api_key": "", "auxiliary_model": None}
        )()
        self.calls = []

    async def stream(self, system, messages, tools):
        self.calls.append({"messages": list(messages)})
        yield ToolCallDelta(id="c1", name="echo_text", arguments={"text": "x"})

    def for_model(self, m):
        return self


class TestDisabledSkills:
    async def test_disabled_skill_not_injected(self):
        """A runner-level disabled skill must not reach the context."""
        from microagent.core.tool import tool

        @tool("echo_text", description="echo")
        async def echo_text(text: str) -> ToolResult:
            return ToolResult.ok(text)

        skills = (
            _FakeSkill("disabled_one", body="DISABLED_BODY_MARKER"),
            _FakeSkill("enabled_one", body="ENABLED_BODY_MARKER"),
        )
        loader = _FakeSkillLoader(skills)
        llm = _EchoLLM()

        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([echo_text]),
            budget=Budget(max_iterations=2),
            skill_loader=loader,
        )
        runner.disabled_skills.add("disabled_one")
        # Simulate both having matched earlier
        runner._loaded_skills["ns:disabled_one"] = None
        runner._loaded_skills["ns:enabled_one"] = None

        async for _ in runner.run_turn([Message.user("test skills")]):
            pass

        assert llm.calls, "LLM was never called"
        sent = "".join(
            str(m.content) for m in llm.calls[0]["messages"] if m.role == "user"
        )
        assert "DISABLED_BODY_MARKER" not in sent
        assert "ENABLED_BODY_MARKER" in sent


# ---------------------------------------------------------------------------
# 10. write_file backup size cap
# ---------------------------------------------------------------------------


class TestWriteFileBackupCap:
    async def test_backup_refused_over_10mb(self, tmp_path):
        from microagent.core.tool import ToolRegistry, _default_builtins
        from microagent.core.types import ToolCall

        big = tmp_path / "big.bin"
        big.write_bytes(b"\x00" * (11 * 1024 * 1024))

        registry = ToolRegistry(_default_builtins())
        tool_fn = registry.get("write_file")
        call = ToolCall(
            id="c1",
            name="write_file",
            arguments={"path": str(big), "content": "new", "backup": True},
        )
        result = await tool_fn.execute(call)
        assert result.is_error
        assert "backup refused" in result.content
        # original file untouched
        assert big.stat().st_size == 11 * 1024 * 1024


# ---------------------------------------------------------------------------
# 11. attachments refuse system paths from untrusted text
# ---------------------------------------------------------------------------


class TestAttachmentsUntrustedSystemPaths:
    def test_user_text_etc_path_rejected(self):
        from microagent.session.attachments import _extract_file_paths

        msgs = (Message.user("please read /etc/hosts for me"),)
        files = _extract_file_paths(msgs)
        assert "/etc/hosts" not in files

    def test_tool_call_arg_etc_path_kept(self):
        from microagent.session.attachments import _extract_file_paths

        msgs = (
            Message.assistant(
                "read",
                tool_calls=(
                    ToolCall(id="c1", name="read_file", arguments={"path": "/etc/hosts"}),
                ),
            ),
        )
        files = _extract_file_paths(msgs)
        assert "/etc/hosts" in files

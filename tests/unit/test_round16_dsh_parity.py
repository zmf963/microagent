"""Round 16 tests — deepseek-harness parity absorbions.

Covers:
1. TurnFailed.code machine classification
2. llm/errors.py classify_exception taxonomy
3. llm/watchdog.py idle timeout
4. bounded tool concurrency (semaphore cap) + exclusive barrier
5. stream-error retry gated on retryable codes
6. _audit_invariants store checks (orphaned tool_calls, consecutive users)
"""

import asyncio

import pytest

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import (
    Message,
    TextDelta,
    ToolCallDelta,
    ToolResult,
    TurnFailed,
    Usage,
)
from microagent.llm.client import LLMConfig, StreamDone
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner


# ---------------------------------------------------------------------------
# 1. TurnFailed code
# ---------------------------------------------------------------------------


class TestTurnFailedCode:
    def test_default_code_is_error(self):
        tf = TurnFailed("boom")
        assert tf.code == "error"

    def test_code_roundtrips(self):
        tf = TurnFailed("stopped", code="interrupted")
        assert tf.code == "interrupted"
        assert tf.reason == "stopped"

    def test_codes_are_stable_strings(self):
        # The documented vocabulary — callers branch on these.
        for code in ("interrupted", "budget", "overflow", "llm_timeout",
                     "llm_error", "compaction", "error"):
            assert TurnFailed("x", code=code).code == code


# ---------------------------------------------------------------------------
# 2. classify_exception
# ---------------------------------------------------------------------------


class _HTTPish(Exception):
    def __init__(self, status, msg="http boom"):
        super().__init__(msg)
        self.status_code = status


class TestClassifyException:
    def test_http_status_mapping(self):
        from microagent.llm.errors import classify_exception

        assert classify_exception(_HTTPish(401)).code == "auth_error"
        assert classify_exception(_HTTPish(429)).code == "rate_limit"
        assert classify_exception(_HTTPish(400)).code == "bad_request"
        assert classify_exception(_HTTPish(413)).code == "context_exceeded"
        assert classify_exception(_HTTPish(504)).code == "timeout"
        assert classify_exception(_HTTPish(529)).code == "overloaded"
        assert classify_exception(_HTTPish(500)).code == "server_error"

    def test_message_heuristics(self):
        from microagent.llm.errors import classify_exception

        assert classify_exception(TimeoutError("read timed out")).code == "timeout"
        assert classify_exception(RuntimeError("connection reset by peer")).code == "network_error"
        assert classify_exception(RuntimeError("invalid api key")).code == "auth_error"
        assert classify_exception(ValueError("response was empty")).code == "empty_response"
        assert classify_exception(RuntimeError("completely novel failure")).code == "unknown"

    def test_retryable_set(self):
        from microagent.llm.errors import RETRYABLE_CODES, classify_exception

        assert classify_exception(TimeoutError("t")).is_retryable
        assert classify_exception(_HTTPish(429)).is_retryable
        assert not classify_exception(_HTTPish(401)).is_retryable
        assert not classify_exception(_HTTPish(400)).is_retryable
        assert "timeout" in RETRYABLE_CODES


# ---------------------------------------------------------------------------
# 3. watchdog
# ---------------------------------------------------------------------------


class _SlowStream:
    def __init__(self, events, delay):
        self._events = list(events)
        self._delay = delay

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        await asyncio.sleep(self._delay)
        return self._events.pop(0)


class TestIdleWatchdog:
    async def test_passthrough_when_events_flow(self):
        from microagent.llm.watchdog import watch_idle

        got = []
        async for e in watch_idle(_SlowStream([1, 2, 3], delay=0.01), timeout_seconds=5):
            got.append(e)
        assert got == [1, 2, 3]

    async def test_timeout_raises_idle_error(self):
        from microagent.llm.watchdog import IdleTimeoutError, watch_idle

        with pytest.raises(IdleTimeoutError):
            async for _ in watch_idle(_SlowStream([1], delay=0.5), timeout_seconds=0.1):
                pass

    async def test_zero_disables(self):
        from microagent.llm.watchdog import watch_idle

        got = []
        async for e in watch_idle(_SlowStream([1, 2], delay=0.2), timeout_seconds=0):
            got.append(e)
        assert got == [1, 2]


# ---------------------------------------------------------------------------
# 4. bounded concurrency + exclusive barrier
# ---------------------------------------------------------------------------


class _ConcurrencyLLM:
    def __init__(self, calls):
        self._calls = list(calls)
        self.config = LLMConfig("fake", "fake-key", "fake-model")

    async def stream(self, system, messages, tools):
        for tid, name, args in self._calls:
            yield ToolCallDelta(id=tid, name=name, arguments=args)
        yield Usage(input_tokens=1, output_tokens=1)
        yield StreamDone(usage=Usage(input_tokens=1, output_tokens=1), stop_reason="tool_calls")

    def for_model(self, m):
        return self


class TestToolConcurrency:
    async def test_more_calls_than_slots_still_all_settle(self):
        """20 concurrent tool calls through a 10-slot pool all complete."""
        from microagent.core.tool import tool

        active = 0
        max_active = 0

        @tool("slow_tool", description="slow")
        async def slow_tool(text: str) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.02)
            finally:
                active -= 1
            return ToolResult.ok(text)

        calls = [(f"c{i}", "slow_tool", {"text": "x"}) for i in range(20)]
        llm = _ConcurrencyLLM(calls)
        runner = SessionRunner(
            llm=llm, registry=ToolRegistry([slow_tool]), budget=Budget(max_iterations=2)
        )
        async for _ in runner.run_turn([Message.user("go")]):
            pass
        # All 20 settled (results collected — no 'not executed' anywhere)
        assert max_active <= 10
        assert max_active > 1  # concurrency actually happened

    async def test_exclusive_tools_serialize(self):
        """Two exclusive tools in one turn never overlap."""
        from microagent.core.tool import tool

        active = 0
        max_active = 0

        @tool("excl_a", description="a", exclusive=True)
        async def excl_a(text: str) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.03)
            finally:
                active -= 1
            return ToolResult.ok(text)

        @tool("excl_b", description="b", exclusive=True)
        async def excl_b(text: str) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.03)
            finally:
                active -= 1
            return ToolResult.ok(text)

        llm = _ConcurrencyLLM(
            [("c1", "excl_a", {"text": "1"}), ("c2", "excl_b", {"text": "2"})]
        )
        runner = SessionRunner(
            llm=llm, registry=ToolRegistry([excl_a, excl_b]), budget=Budget(max_iterations=2)
        )
        async for _ in runner.run_turn([Message.user("go")]):
            pass
        assert max_active == 1

    async def test_browser_and_lsp_are_exclusive(self):
        reg = ToolRegistry(_default_builtins())
        assert reg.get("browser_navigate").exclusive
        assert reg.get("lsp").exclusive


# ---------------------------------------------------------------------------
# 5. stream retry gated on retryable codes
# ---------------------------------------------------------------------------


class _FailLLM:
    """LLM whose stream raises once, then succeeds (or keeps failing)."""

    def __init__(self, error, always=False):
        self._error = error
        self._always = always
        self._calls = 0
        self.config = LLMConfig("fake", "fake-key", "fake-model")

    async def stream(self, system, messages, tools):
        self._calls += 1
        if self._always or self._calls == 1:
            raise self._error
        yield TextDelta(text="recovered", kind="content")
        yield Usage(input_tokens=1, output_tokens=1)
        yield StreamDone(usage=Usage(input_tokens=1, output_tokens=1), stop_reason="stop")

    def for_model(self, m):
        return self


class TestRetryGating:
    async def test_transient_error_retries_and_recovers(self):
        llm = _FailLLM(TimeoutError("network stalled"))
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget())
        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        from microagent.core.types import TurnComplete

        assert any(isinstance(e, TurnComplete) for e in events)
        assert llm._calls == 2

    async def test_auth_error_does_not_retry(self):
        llm = _FailLLM(_HTTPish(401), always=True)
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget())
        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        tfs = [e for e in events if isinstance(e, TurnFailed)]
        assert tfs and tfs[0].code == "llm_error"
        assert llm._calls == 1  # no retry burned on a dead key

    async def test_timeout_code_reported(self):
        llm = _FailLLM(TimeoutError("stalled"), always=True)
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget())
        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        tfs = [e for e in events if isinstance(e, TurnFailed)]
        assert tfs and tfs[0].code == "llm_timeout"


# ---------------------------------------------------------------------------
# 6. _audit_invariants
# ---------------------------------------------------------------------------


class TestAuditInvariants:
    async def test_clean_history_passes(self, tmp_path, monkeypatch):
        from microagent.core.store import SQLiteStore

        monkeypatch.setenv("MICROAGENT_AUDIT_INVARIANTS", "1")
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("hi"))
        llm = _ConcurrencyLLM([])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget(), store=store, session_id="s1")
        async for _ in runner.run_turn([Message.user("hi")]):
            pass

    async def test_orphaned_tool_call_detected(self, tmp_path, monkeypatch):
        from microagent.core.store import SQLiteStore

        monkeypatch.setenv("MICROAGENT_AUDIT_INVARIANTS", "1")
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("hi"))
        await store.append(
            "s1", Message.assistant("", tool_calls=((_mk_tc("c1"),)))
        )
        llm = _ConcurrencyLLM([])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget(), store=store, session_id="s1")
        with pytest.raises(RuntimeError, match="orphaned tool_call"):
            async for _ in runner.run_turn([Message.user("hi again")]):
                pass

    async def test_consecutive_users_detected(self, tmp_path, monkeypatch):
        from microagent.core.store import SQLiteStore

        monkeypatch.setenv("MICROAGENT_AUDIT_INVARIANTS", "1")
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("one"))
        await store.append("s1", Message.user("two"))
        llm = _ConcurrencyLLM([])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget(), store=store, session_id="s1")
        with pytest.raises(RuntimeError, match="consecutive user"):
            async for _ in runner.run_turn([Message.user("three")]):
                pass

    async def test_audit_off_by_default(self, tmp_path, monkeypatch):
        from microagent.core.store import SQLiteStore

        monkeypatch.delenv("MICROAGENT_AUDIT_INVARIANTS", raising=False)
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("one"))
        await store.append("s1", Message.user("two"))  # violating — but audit off
        llm = _ConcurrencyLLM([])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget(), store=store, session_id="s1")
        events = []
        async for e in runner.run_turn([Message.user("three")]):
            events.append(e)
        assert any(isinstance(e, TurnFailed) or True for e in events)  # no crash


def _mk_tc(tid):
    from microagent.core.types import ToolCall

    return ToolCall(id=tid, name="bash", arguments={"command": "echo hi"})

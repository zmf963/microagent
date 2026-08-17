"""v1.1.1 tests — flush barrier, per-provider retry policy, bash TerminalBackend seam."""

import asyncio

import pytest

from microagent.core.store import InMemoryStore, SQLiteStore
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, TextDelta, ToolCallDelta, Usage
from microagent.llm.client import LLMConfig, StreamDone
from microagent.llm.retry import RetryPolicy
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner


# ---------------------------------------------------------------------------
# C: flush barrier
# ---------------------------------------------------------------------------


class TestFlushBarrier:
    async def test_flush_sqlite_works(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        await store.append("s1", Message.user("hi"))
        await store.flush("s1")  # must not raise

    async def test_flush_inmemory_noop(self):
        store = InMemoryStore()
        await store.flush("s1")  # must not raise

    async def test_turn_complete_flushes_store(self, tmp_path):
        """A turn with a store triggers flush before TurnComplete."""

        class _TrackingStore(InMemoryStore):
            def __init__(self):
                super().__init__()
                self.flushed: list[str] = []

            async def flush(self, session_id: str) -> None:
                self.flushed.append(session_id)

        class _LLM:
            def __init__(self):
                self.config = LLMConfig("fake", "k", "m")

            async def stream(self, system, messages, tools):
                yield TextDelta(text="done", kind="content")
                yield Usage()
                yield StreamDone(usage=Usage(), stop_reason="stop")

            def for_model(self, m):
                return self

        store = _TrackingStore()
        runner = SessionRunner(
            llm=_LLM(), registry=ToolRegistry([]), budget=Budget(),
            store=store, session_id="s1",
        )
        from microagent.core.types import TurnComplete

        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        assert any(isinstance(e, TurnComplete) for e in events)
        assert store.flushed == ["s1"]

    async def test_flush_failure_does_not_kill_turn(self):
        """A raising flush must not swallow the TurnComplete."""

        class _BoomStore(InMemoryStore):
            async def flush(self, session_id: str) -> None:
                raise RuntimeError("disk full")

        class _LLM:
            def __init__(self):
                self.config = LLMConfig("fake", "k", "m")

            async def stream(self, system, messages, tools):
                yield TextDelta(text="done", kind="content")
                yield Usage()
                yield StreamDone(usage=Usage(), stop_reason="stop")

            def for_model(self, m):
                return self

        runner = SessionRunner(
            llm=_LLM(), registry=ToolRegistry([]), budget=Budget(),
            store=_BoomStore(), session_id="s1",
        )
        from microagent.core.types import TurnComplete

        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        assert any(isinstance(e, TurnComplete) for e in events)


# ---------------------------------------------------------------------------
# D: RetryPolicy
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    def test_default_normal_allows_one(self):
        p = RetryPolicy()
        assert p.allows_retry("timeout", 0)
        assert not p.allows_retry("timeout", 1)

    def test_always_caps_at_max(self):
        p = RetryPolicy(mode="always", max_retries=3)
        assert p.allows_retry("timeout", 0)
        assert p.allows_retry("timeout", 2)
        assert not p.allows_retry("timeout", 3)

    def test_never_denies_all(self):
        p = RetryPolicy(mode="never")
        assert not p.allows_retry("timeout", 0)

    def test_non_retryable_always_denied(self):
        p = RetryPolicy(mode="always", max_retries=5)
        assert not p.allows_retry("auth_error", 0)
        assert not p.allows_retry("bad_request", 0)

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            RetryPolicy(mode="sometimes")

    def test_from_str(self):
        assert RetryPolicy.from_str("normal").mode == "normal"
        assert RetryPolicy.from_str("always:5").max_retries == 5
        assert RetryPolicy.from_str("always").max_retries == 3
        assert RetryPolicy.from_str("never").mode == "never"
        assert RetryPolicy.from_str("garbage").mode == "normal"

    def test_config_resolves_policy(self):
        cfg = LLMConfig("fake", "k", "m", retry_policy="never")
        assert cfg.resolved_retry_policy().mode == "never"
        cfg2 = LLMConfig("fake", "k", "m", retry_policy=RetryPolicy(mode="always", max_retries=2))
        assert cfg2.resolved_retry_policy().max_retries == 2

    def test_exported(self):
        import microagent

        assert hasattr(microagent, "RetryPolicy")


class TestRunnerRetryPolicy:
    async def test_never_policy_skips_retry(self):
        class _FailLLM:
            def __init__(self):
                self._calls = 0
                self.config = LLMConfig("fake", "k", "m", retry_policy="never")

            async def stream(self, system, messages, tools):
                self._calls += 1
                # raise inside an async generator body (after yield point
                # is never reached) so stream IS a generator that raises
                if True:
                    raise TimeoutError("stalled")
                yield  # pragma: no cover

            def for_model(self, m):
                return self

        llm = _FailLLM()
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget())
        from microagent.core.types import TurnFailed

        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        tfs = [e for e in events if isinstance(e, TurnFailed)]
        assert tfs and tfs[0].code == "llm_timeout"
        assert llm._calls == 1  # never retried

    async def test_normal_policy_retries_once(self):
        class _FailOnceLLM:
            def __init__(self):
                self._calls = 0
                self.config = LLMConfig("fake", "k", "m", retry_policy="normal")

            async def stream(self, system, messages, tools):
                self._calls += 1
                if self._calls == 1:
                    raise TimeoutError("stalled")
                yield TextDelta(text="recovered", kind="content")
                yield Usage()
                yield StreamDone(usage=Usage(), stop_reason="stop")

            def for_model(self, m):
                return self

        llm = _FailOnceLLM()
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget())
        from microagent.core.types import TurnComplete

        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        assert any(isinstance(e, TurnComplete) for e in events)
        assert llm._calls == 2


# ---------------------------------------------------------------------------
# B: bash TerminalBackend seam
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def run(self, command, *, cwd=None, env=None, timeout=None):
        self.calls.append((command, timeout))
        return self._result


class TestBashBackendSeam:
    async def test_unbound_backend_uses_local_path(self):
        from microagent.core.tool import ToolRegistry, _default_builtins
        from microagent.core.types import ToolCall

        reg = ToolRegistry(_default_builtins())
        r = await reg.get("bash").execute(
            ToolCall(id="c1", name="bash", arguments={"command": "echo hello"})
        )
        assert not r.is_error
        assert "hello" in r.content

    async def test_bound_backend_routes_calls(self):
        from microagent.tools.builtins.bash import bash, set_backend
        from microagent.terminal.backend import TerminalResult

        backend = _FakeBackend(TerminalResult.ok("from-docker"))
        token = None
        try:
            set_backend(backend)
            r = await bash.fn(command="echo hi", timeout=10)
        finally:
            set_backend(None)
        assert "from-docker" in r.content
        assert backend.calls == [("echo hi", 10)]

    async def test_backend_error_maps_to_tool_error(self):
        from microagent.tools.builtins.bash import bash, set_backend
        from microagent.terminal.backend import TerminalResult

        backend = _FakeBackend(TerminalResult.ok("", "boom", exit_code=127))
        try:
            set_backend(backend)
            r = await bash.fn(command="nope", timeout=10)
        finally:
            set_backend(None)
        assert r.is_error
        assert "127" in r.content

    async def test_backend_timeout_maps_to_timeout_error(self):
        from microagent.tools.builtins.bash import bash, set_backend
        from microagent.terminal.backend import TerminalResult

        backend = _FakeBackend(TerminalResult.ok("", "slow", exit_code=-1, timed_out=True))
        try:
            set_backend(backend)
            r = await bash.fn(command="sleep 100", timeout=1)
        finally:
            set_backend(None)
        assert r.is_error
        assert "timed out" in r.content

    async def test_backend_exception_maps_to_error(self):
        from microagent.tools.builtins.bash import bash, set_backend

        class _BoomBackend:
            async def run(self, command, *, cwd=None, env=None, timeout=None):
                raise ConnectionError("ssh lost")

        try:
            set_backend(_BoomBackend())
            r = await bash.fn(command="ls", timeout=10)
        finally:
            set_backend(None)
        assert r.is_error
        assert "ssh lost" in r.content

    async def test_runner_binds_backend_per_session(self, tmp_path):
        """SessionRunner(terminal_backend=...) routes bash through it."""
        from microagent.terminal.backend import TerminalResult

        backend = _FakeBackend(TerminalResult.ok("from-session-backend"))

        class _LLM:
            def __init__(self):
                self.config = LLMConfig("fake", "k", "m")

            async def stream(self, system, messages, tools):
                yield ToolCallDelta(id="c1", name="bash", arguments={"command": "echo x"})
                yield Usage()
                yield StreamDone(usage=Usage(), stop_reason="tool_calls")

            def for_model(self, m):
                return self

        runner = SessionRunner(
            llm=_LLM(), registry=ToolRegistry(_default_builtins()),
            budget=Budget(max_iterations=2), terminal_backend=backend,
        )
        async for _ in runner.run_turn([Message.user("run")]):
            pass
        assert backend.calls, "backend never received the bash call"

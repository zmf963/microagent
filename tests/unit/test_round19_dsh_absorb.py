"""Round 19 tests — deepseek-harness absorb-now batch (v1.1.0).

Covers:
1. ABORTED_BEFORE_DISPATCH / ABORTED metadata codes on interrupted tools
2. UnsupportedSessionError: unknown non-ignorable kind fails loud; ignorable skips
3. llm_retry ledger: record + last lookup (code-filtered), InMemoryStore parity
4. LLMFailure retry_after_ms / request_id extraction from headers
5. ToolResult.error(metadata=...) constructor
"""

import asyncio

import pytest

from microagent.core.store import InMemoryStore, SQLiteStore, UnsupportedSessionError
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, ToolCallDelta, ToolResult
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner


# ---------------------------------------------------------------------------
# 1. abort codes
# ---------------------------------------------------------------------------


class TestAbortCodes:
    async def test_before_dispatch_code_on_interrupt(self):
        """A tool cancelled before its body ran gets ABORTED_BEFORE_DISPATCH."""
        from microagent.core.tool import tool

        @tool("slow_a", description="slow")
        async def slow_a(command: str) -> ToolResult:
            await asyncio.sleep(10)
            return ToolResult.ok("done")

        class _LLM:
            def __init__(self):
                self.config = type(
                    "C", (), {"model": "test", "base_url": "", "api_key": "", "auxiliary_model": None}
                )()

            async def stream(self, system, messages, tools):
                yield ToolCallDelta(id="c1", name="slow_a", arguments={"command": "x"})

            def for_model(self, m):
                return self

        runner = SessionRunner(
            llm=_LLM(), registry=ToolRegistry([slow_a]), budget=Budget()
        )
        # Patch _run_tool_calls to observe results directly
        original = runner._run_tool_calls
        observed = []

        async def _spy(calls):
            task = asyncio.create_task(original(calls))
            await asyncio.sleep(0.3)  # tool inside its body sleep
            runner._interrupt_requested = True  # simulate watcher flip
            results, progress = await task
            observed.extend(results)
            return results, progress

        runner._run_tool_calls = _spy

        from microagent.core.types import ToolCall

        async for _ in runner.run_turn([Message.user("go")]):
            pass
        assert observed
        # The slow_a tool entered its body (sleep started) → ABORTED
        assert observed[0].is_error
        assert observed[0].metadata.get("code") in (
            "ABORTED", "ABORTED_BEFORE_DISPATCH",
        )

    async def test_before_dispatch_for_waiting_on_slots(self):
        """A tool waiting on the concurrency slots never invokes its body."""
        from microagent.core.tool import tool

        # 15 tools, pool cap 10 — the last 5 wait on the semaphore and
        # are cancelled before dispatch when interrupt fires fast.
        invoked = []

        @tool("track_tool", description="tracks invocation")
        async def track_tool(text: str) -> ToolResult:
            invoked.append(text)
            await asyncio.sleep(0.2)
            return ToolResult.ok(text)

        class _LLM:
            def __init__(self):
                self.config = type(
                    "C", (), {"model": "test", "base_url": "", "api_key": "", "auxiliary_model": None}
                )()

            async def stream(self, system, messages, tools):
                for i in range(15):
                    yield ToolCallDelta(id=f"c{i}", name="track_tool", arguments={"text": str(i)})

            def for_model(self, m):
                return self

        runner = SessionRunner(
            llm=_LLM(), registry=ToolRegistry([track_tool]), budget=Budget()
        )
        original = runner._run_tool_calls
        observed = []

        async def _spy(calls):
            task = asyncio.create_task(original(calls))
            await asyncio.sleep(0.05)  # first ~10 enter, rest wait on slots
            runner._interrupt_requested = True
            results, progress = await task
            observed.extend(results)
            return results, progress

        runner._run_tool_calls = _spy

        async for _ in runner.run_turn([Message.user("go")]):
            pass

        codes = [r.metadata.get("code") for r in observed if r.metadata]
        assert "ABORTED_BEFORE_DISPATCH" in codes  # some never ran
        assert "ABORTED" in codes  # some ran then cancelled


# ---------------------------------------------------------------------------
# 2. UnsupportedSessionError
# ---------------------------------------------------------------------------


class TestUnsupportedSessionError:
    async def test_unknown_non_ignorable_kind_raises(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        # Insert a future-version row directly
        import json

        store._conn.execute(
            "INSERT INTO messages (session_id, seq, data) VALUES (?, ?, ?)",
            ("fut", 1, json.dumps({"role": "user", "content": "x", "kind": "future_event"})),
        )
        with pytest.raises(UnsupportedSessionError):
            await store.load_history("fut")

    async def test_ignorable_kind_skips(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        import json

        store._conn.execute(
            "INSERT INTO messages (session_id, seq, data) VALUES (?, ?, ?)",
            ("fut", 1, json.dumps({
                "role": "user", "content": "x",
                "kind": "future_event", "ignorable": True,
            })),
        )
        hist = await store.load_history("fut")
        assert len(hist) == 1  # loaded as a normal message

    async def test_legacy_rows_without_kind_still_load(self, tmp_path):
        """Old stores without the kind field remain readable."""
        store = SQLiteStore(tmp_path / "s.db")
        import json

        store._conn.execute(
            "INSERT INTO messages (session_id, seq, data) VALUES (?, ?, ?)",
            ("old", 1, json.dumps({"role": "user", "content": "legacy"})),
        )
        hist = await store.load_history("old")
        assert hist[0].content == "legacy"


# ---------------------------------------------------------------------------
# 3. llm_retry ledger
# ---------------------------------------------------------------------------


class TestRetryLedger:
    async def test_record_and_last_lookup(self, tmp_path):
        store = SQLiteStore(tmp_path / "s.db")
        assert await store.last_llm_retry("s1") is None
        await store.record_llm_retry("s1", "timeout", 1000)
        await store.record_llm_retry("s1", "rate_limit", 2500)
        last = await store.last_llm_retry("s1")
        assert last == ("rate_limit", 2500)
        # code-filtered
        assert await store.last_llm_retry("s1", "timeout") == ("timeout", 1000)
        assert await store.last_llm_retry("s1", "server_error") is None

    async def test_inmemory_parity(self):
        store = InMemoryStore()
        assert await store.last_llm_retry("s1") is None
        await store.record_llm_retry("s1", "timeout", 500)
        assert await store.last_llm_retry("s1") == ("timeout", 500)
        assert await store.last_llm_retry("s1", "timeout") == ("timeout", 500)

    async def test_runner_records_retry_on_transient_failure(self, tmp_path):
        """A transient stream failure writes to the ledger before retrying."""
        class _FailOnceLLM:
            def __init__(self):
                self._calls = 0
                self.config = type(
                    "C", (), {"model": "test", "base_url": "", "api_key": "", "auxiliary_model": None}
                )()

            async def stream(self, system, messages, tools):
                self._calls += 1
                if self._calls == 1:
                    raise TimeoutError("stalled")
                from microagent.core.types import TextDelta, Usage
                from microagent.llm.client import StreamDone

                yield TextDelta(text="recovered", kind="content")
                yield Usage()
                yield StreamDone(usage=Usage(), stop_reason="stop")

            def for_model(self, m):
                return self

        store = SQLiteStore(tmp_path / "s.db")
        runner = SessionRunner(
            llm=_FailOnceLLM(), registry=ToolRegistry([]), budget=Budget(),
            store=store, session_id="s1",
        )
        async for _ in runner.run_turn([Message.user("hi")]):
            pass
        last = await store.last_llm_retry("s1")
        assert last is not None and last[0] == "timeout"


# ---------------------------------------------------------------------------
# 4. LLMFailure hints
# ---------------------------------------------------------------------------


class TestLLMFailureHints:
    def test_retry_after_and_request_id_extracted(self):
        from microagent.llm.errors import classify_exception

        class _WithHeaders(Exception):
            def __init__(self):
                super().__init__("rate limited")
                self.status_code = 429
                self.headers = {
                    "Retry-After": "3",
                    "x-request-id": "req-abc-123",
                }

        f = classify_exception(_WithHeaders())
        assert f.code == "rate_limit"
        assert f.retry_after_ms == 3000
        assert f.request_id == "req-abc-123"

    def test_no_hints_returns_none(self):
        from microagent.llm.errors import classify_exception

        f = classify_exception(TimeoutError("t"))
        assert f.retry_after_ms is None
        assert f.request_id is None


# ---------------------------------------------------------------------------
# 5. ToolResult.error metadata
# ---------------------------------------------------------------------------


class TestToolResultErrorMetadata:
    def test_metadata_kwarg(self):
        r = ToolResult.error("boom", metadata={"code": "ABORTED"})
        assert r.is_error
        assert r.metadata == {"code": "ABORTED"}

    def test_default_none(self):
        r = ToolResult.error("boom")
        assert r.metadata is None

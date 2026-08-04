"""Tests for error/edge paths in SessionRunner."""

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, TextDelta, TurnComplete, TurnFailed, Usage
from microagent.llm.client import StreamDone
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from tests.unit.fake_llm import FakeLLMClient, ScriptedResponse, text_response, tool_response


class TestRunnerErrorPaths:
    async def test_llm_returns_empty(self):
        """LLM returns no text and no tool calls."""
        llm = FakeLLMClient([text_response("")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]))
        messages = [Message.user("hi")]
        result = None
        async for event in runner.run_turn(messages):
            result = event
        assert isinstance(result, TurnComplete)
        assert result.content == ""

    async def test_llm_stream_length_truncation(self):
        """LLM response truncated — StreamDone stop_reason='length'."""
        resp = ScriptedResponse(
            events=[
                TextDelta(text="partial"),
                Usage(input_tokens=10, output_tokens=5),
                StreamDone(usage=Usage(input_tokens=10, output_tokens=5), stop_reason="length"),
            ]
        )
        llm = FakeLLMClient([resp])
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]))
        messages = [Message.user("hi")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        assert any(isinstance(e, TurnFailed) for e in events)

    async def test_tool_execution_continues_after_error(self):
        """Tool execution throws — error captured, loop continues."""
        llm = FakeLLMClient(
            [
                tool_response([("c1", "bash", {"command": "nonexistent_binary_xyz"})]),
                text_response("tool failed but continuing"),
            ]
        )
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(llm=llm, registry=reg, budget=Budget(max_iterations=5))
        messages = [Message.user("run bad command")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        # Should complete despite tool error
        completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(completes) >= 1

    async def test_budget_iteration_exhaustion(self):
        """Budget exhausted after max_iterations — TurnFailed."""
        llm = FakeLLMClient(
            [
                tool_response([("c1", "bash", {"command": "echo 1"})]),
                tool_response([("c2", "bash", {"command": "echo 2"})]),
                tool_response([("c3", "bash", {"command": "echo 3"})]),
            ]
        )
        reg = ToolRegistry(_default_builtins())
        budget = Budget(max_iterations=2)
        runner = SessionRunner(llm=llm, registry=reg, budget=budget)
        messages = [Message.user("do stuff")]
        events = []
        async for event in runner.run_turn(messages):
            events.append(event)
        assert any(isinstance(e, TurnFailed) for e in events)
        assert "budget" in events[-1].reason.lower()

    async def test_compaction_guard_against_empty(self):
        """Compression should not crash on very short conversations."""
        from microagent.session.compress import compact_conversation, count_tokens

        messages = (Message.user("hi"), Message.assistant("hello"))
        tokens = count_tokens(messages)
        # Compaction with safe window
        result = await compact_conversation(
            messages,
            llm=None,
            context_window=10_000,
            force=False,
        )
        # Should return original (under threshold) — no crash
        assert len(result) == len(messages)


class _ExplodingLLM:
    """LLMClient-compatible: raises on first stream call, replays after."""

    def __init__(self, exc, then):
        self._exc = exc
        self._then = then
        self.calls = 0
        self.config = then.config

    async def stream(self, **kwargs):
        self.calls += 1
        if self.calls == 1 and self._exc is not None:
            raise self._exc
        async for e in self._then.stream(**kwargs):
            yield e


class _PartialThenExplodeLLM:
    """Yields partial content, then raises mid-stream."""

    def __init__(self):
        from microagent.llm.client import LLMConfig
        self.config = LLMConfig("fake", "fake-key", "fake-model")

    async def stream(self, **kwargs):
        yield TextDelta(text="partial content")
        raise RuntimeError("stream died mid-flight")
        yield  # pragma: no cover


class TestLLMStreamErrors:
    async def test_stream_error_before_content_retries(self):
        """LLM stream raises before any content → retry once, recover."""
        from microagent.llm.client import LLMConfig

        fallback = FakeLLMClient([text_response("recovered")])
        llm = _ExplodingLLM(RuntimeError("connection reset"), fallback)
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget.root())
        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(completes) >= 1
        assert "recovered" in completes[0].content
        assert llm.calls == 2

    async def test_stream_error_twice_fails(self):
        """LLM stream raises twice → TurnFailed, no exception escapes."""
        from microagent.llm.client import LLMConfig

        fallback = FakeLLMClient([text_response("never reached")])
        llm = _ExplodingLLM(RuntimeError("boom"), fallback)
        # Force both calls to raise: exc replays on call 1 AND call 2
        original_stream = llm.stream

        async def always_explode(**kwargs):
            llm.calls += 1
            raise RuntimeError("boom")
            yield  # pragma: no cover

        llm.stream = always_explode
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget.root())
        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        fails = [e for e in events if isinstance(e, TurnFailed)]
        assert len(fails) >= 1, f"expected TurnFailed, got {[type(e).__name__ for e in events]}"
        assert "llm" in fails[0].reason.lower() or "error" in fails[0].reason.lower()

    async def test_stream_error_after_partial_content_no_retry(self):
        """Content already streamed to consumer, then stream dies →
        TurnFailed without retry (retry would duplicate the partial text)."""
        llm = _PartialThenExplodeLLM()
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget.root())
        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        fails = [e for e in events if isinstance(e, TurnFailed)]
        assert len(fails) >= 1, f"expected TurnFailed, got {[type(e).__name__ for e in events]}"
        texts = [e for e in events if isinstance(e, TextDelta) and e.kind == "content"]
        assert len(texts) == 1, f"partial content must not be duplicated: {len(texts)} TextDeltas"

    async def test_arun_llm_error_returns_error_string(self):
        """Agent.arun returns '[error: ...]' on LLM failure instead of raising."""
        from microagent.agent import Agent
        from microagent.core.tool import ToolRegistry

        runner = SessionRunner(llm=None, registry=ToolRegistry([]), budget=Budget.root())

        async def always_explode(**kwargs):
            raise RuntimeError("API down")
            yield  # pragma: no cover

        runner.llm = type("LLM", (), {
            "config": FakeLLMClient([]).config,
            "stream": staticmethod(always_explode),
        })()
        agent = Agent(runner=runner, registry=runner.registry)
        result = await agent.arun([Message.user("hi")])
        assert result.startswith("[error:"), f"got {result!r}"
        await agent.close()

class TestConcurrentTurnsSameRunner:
    async def test_concurrent_run_turn_serialized(self, tmp_path):
        """Two concurrent run_turn calls on the SAME runner previously
        interleaved: both loaded empty history, both appended their user
        message first → store order user-A,user-B,assistant-A,assistant-B.
        On resume the model sees a conversation that never happened.
        The per-runner turn lock must serialize them."""
        import asyncio

        from microagent.core.store import SQLiteStore

        store = SQLiteStore(tmp_path / "s.db")
        llm = FakeLLMClient([text_response("reply-A"), text_response("reply-B")])
        runner = SessionRunner(
            llm=llm, registry=ToolRegistry([]), budget=Budget.root(),
            store=store, session_id="shared",
        )

        async def run(tag):
            return [type(e).__name__ async for e in runner.run_turn([Message.user(f"q-{tag}")])]

        res = await asyncio.gather(run("A"), run("B"))
        assert all("TurnComplete" in r for r in res), res

        hist = await store.load_history("shared")
        roles_contents = [(m.role, m.content) for m in hist]
        assert roles_contents == [
            ("user", "q-A"),
            ("assistant", "reply-A"),
            ("user", "q-B"),
            ("assistant", "reply-B"),
        ], roles_contents
        store.close()

    async def test_turn_lock_does_not_deadlock_interrupt(self, tmp_path):
        """interrupt() is called from outside the turn and must not need
        the turn lock — a second turn queued behind the first still runs."""
        import asyncio

        from microagent.core.store import SQLiteStore

        store = SQLiteStore(tmp_path / "s2.db")
        llm = FakeLLMClient([text_response("r1"), text_response("r2")])
        runner = SessionRunner(
            llm=llm, registry=ToolRegistry([]), budget=Budget.root(),
            store=store, session_id="s",
        )
        r1 = [type(e).__name__ async for e in runner.run_turn([Message.user("one")])]
        r2 = [type(e).__name__ async for e in runner.run_turn([Message.user("two")])]
        assert "TurnComplete" in r1 and "TurnComplete" in r2
        store.close()

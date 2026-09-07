"""v1.2.0 Phase 2: surfaceOp replace-fold event sourcing (dsh parity).

Compaction used to mutate only the in-memory message list — the store
kept the raw log and nothing recorded the fold. Resuming a compacted
session reloaded the FULL uncompacted history and burned another L3 LLM
call to re-compress, with the incremental-summary chain (previous_summary,
per-runner state) broken across restarts.

Now every structural compaction (auto L3 / overflow force / manual
/compact / breaker fallback) persists:
  - the inserted block (placeholder/attachments + summary) as real
    messages at the log tail, and
  - a surface_ops row: shadowed range [start_seq..end_seq] → block
    [repl_start..repl_end], kind 'summary' | 'fallback'.
load_surface() derives the LLM-visible surface by applying folds in
order (double-checked per fold); resume uses it and rehydrates
previous_summary from the last 'summary' fold.
"""

from microagent.core.store import InMemoryStore, SQLiteStore
from microagent.core.types import Message


async def _seed(store, sid="s1", n=6):
    for i in range(n):
        await store.append(sid, Message.user(f"msg-{i}"))
    return store


class TestStoreFolds:
    async def test_no_folds_surface_equals_history(self, tmp_path):
        store = SQLiteStore(tmp_path / "t.db")
        await _seed(store)
        surface = await store.load_surface("s1")
        raw = await store.load_history("s1")
        assert [m.content for m in surface] == [m.content for m in raw]

    async def test_fold_shadows_range_and_inserts_block(self, tmp_path):
        store = SQLiteStore(tmp_path / "t.db")
        await _seed(store)  # seqs 1..6
        # Simulated compaction: shadow 1..4, replacement = one summary msg.
        seq = await store.append("s1", Message.user("SUMMARY"))
        assert seq == 7
        assert seq is not None
        await store.record_fold("s1", "summary", 1, 4, seq, seq)
        surface = await store.load_surface("s1")
        assert [m.content for m in surface] == [
            "SUMMARY", "msg-4", "msg-5",
        ]
        # Raw log untouched — event-sourcing invariant.
        raw = await store.load_history("s1")
        assert len(raw) == 7

    async def test_nested_folds_apply_in_order(self, tmp_path):
        store = SQLiteStore(tmp_path / "t.db")
        await _seed(store)  # 1..6
        await store.append("s1", Message.user("S1"))  # 7
        await store.record_fold("s1", "summary", 1, 3, 7, 7)
        # Turn continues: assistant + tool messages appended (8, 9).
        await store.append("s1", Message.assistant("a"))
        await store.append("s1", Message.user("u2"))
        # Second fold shadows [7 (S1), 9 (u2)] with S2 (10).
        await store.append("s1", Message.user("S2"))  # 10
        await store.record_fold("s1", "summary", 7, 9, 10, 10)
        surface = await store.load_surface("s1")
        assert [m.content for m in surface] == [
            "S2", "msg-3", "msg-4", "msg-5",
        ]

    async def test_multi_message_block(self, tmp_path):
        """L3 inserts attachments BEFORE the summary — the whole block
        moves into the gap in order."""
        store = SQLiteStore(tmp_path / "t.db")
        await _seed(store)  # 1..6
        a = await store.append("s1", Message.user("attach"))  # 7
        b = await store.append("s1", Message.user("SUMMARY"))  # 8
        assert a is not None and b is not None
        await store.record_fold("s1", "summary", 1, 5, a, b)
        surface = await store.load_surface("s1")
        assert [m.content for m in surface] == ["attach", "SUMMARY", "msg-5"]

    async def test_invalid_fold_skipped_not_fatal(self, tmp_path):
        store = SQLiteStore(tmp_path / "t.db")
        await _seed(store)
        await store.record_fold("s1", "summary", 99, 100, 101, 102)  # missing
        await store.record_fold("s1", "summary", 3, 2, 5, 4)  # start > end
        surface = await store.load_surface("s1")
        assert len(surface) == 6  # untouched

    async def test_last_fold_summary(self, tmp_path):
        store = SQLiteStore(tmp_path / "t.db")
        await _seed(store)
        await store.append("s1", Message.user("S1"))
        await store.record_fold("s1", "summary", 1, 3, 7, 7)
        await store.append("s1", Message.user("PLACEHOLDER"))
        await store.record_fold("s1", "fallback", 4, 5, 8, 8)
        m = await store.last_fold_summary("s1")
        assert m is not None and m.content == "S1"  # last SUMMARY, not fallback

    async def test_inmemory_mirror(self):
        store = InMemoryStore()
        await _seed(store)
        s = await store.append("s1", Message.user("SUMMARY"))
        assert s is not None
        await store.record_fold("s1", "summary", 1, 4, s, s)
        surface = await store.load_surface("s1")
        assert [m.content for m in surface] == ["SUMMARY", "msg-4", "msg-5"]

    async def test_append_returns_seq(self, tmp_path):
        store = SQLiteStore(tmp_path / "t.db")
        assert await store.append("s1", Message.user("a")) == 1
        assert await store.append("s1", Message.user("b")) == 2
        assert await store.append("s2", Message.user("c")) == 1  # per-session


class TestRunnerFoldRecording:
    """End-to-end: auto-compaction on a store-backed session records a
    fold; resume derives the compacted surface WITHOUT re-compressing."""

    async def test_full_cycle(self, tmp_path):
        """Drive a real compaction through run_turn with a store, then
        resume on a FRESH runner and verify: (a) surface is compacted,
        (b) previous_summary rehydrated, (c) no re-compression LLM call."""
        from microagent.core.tool import ToolRegistry
        from microagent.core.types import (
            Message,
            TextDelta,
            ToolCallDelta,
            TurnComplete,
        )
        from microagent.llm.client import StreamDone, Usage
        from microagent.session.runner import SessionRunner

        from .fake_llm import FakeLLMClient, ScriptedResponse

        class _CompressLLM:
            """Main LLM: one text turn. Compactor: yields a summary."""

            def __init__(self):
                self.config = type(
                    "C",
                    (),
                    {
                        "model": "fake-model",
                        "base_url": "",
                        "api_key": "",
                        "auxiliary_model": None,
                    },
                )()
                self.compress_calls = 0

            async def stream(self, *, system, messages, tools=None):
                from microagent.core.types import TextDelta

                if "compressor" in system:
                    self.compress_calls += 1
                    yield TextDelta(text="<summary>compressed once</summary>", kind="content")
                    yield Usage(input_tokens=1, output_tokens=1)
                    yield StreamDone(usage=Usage(input_tokens=1, output_tokens=1), stop_reason="stop")
                    return
                yield TextDelta(text="hello answer", kind="content")
                yield Usage(input_tokens=10, output_tokens=5)
                yield StreamDone(usage=Usage(input_tokens=10, output_tokens=5), stop_reason="stop")

            def for_model(self, m):
                return self

        store = SQLiteStore(tmp_path / "s.db")
        for i in range(8):
            await store.append("s1", Message.user(f"backing-{i} " + "x" * 120))
        llm = _CompressLLM()
        runner = SessionRunner(
            llm=llm, registry=ToolRegistry(), store=store, session_id="s1",
            compression_threshold=200,
        )
        # Realistic flow: messages come from resume() (aligned seq sidecar).
        msgs = list(await runner.resume("s1", store))
        assert len(msgs) == 8
        async for _ in runner.run_turn(msgs):
            pass
        await runner.close()

        assert llm.compress_calls == 1, "auto compaction should have run once"
        surface_now = await store.load_surface("s1")
        assert any("compressed once" in m.content for m in surface_now)
        assert len(surface_now) < len(await store.load_history("s1"))

        # Fresh runner resumes: surface derived, chain rehydrated, and NO
        # extra compressor call on the next small turn.
        runner2 = SessionRunner(
            llm=llm, registry=ToolRegistry(), store=store, session_id="s1",
            compression_threshold=200,
        )
        resumed = list(await runner2.resume("s1", store))
        assert runner2._compaction_state.previous_summary == "compressed once"
        calls_before = llm.compress_calls
        msgs2 = resumed + [Message.user("small follow-up")]
        done = False
        async for _ in runner2.run_turn(msgs2):
            done = True
        assert done
        assert llm.compress_calls == calls_before, (
            "resume must not re-compress — the fold already derives the surface"
        )
        assert any("hello answer" in m.content for m in msgs2)
        await runner2.close()

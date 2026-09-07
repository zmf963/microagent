"""v1.2.0 Phase 1: ToolProgressDelta true streaming.

Progress deltas used to be buffered until EVERY tool in the batch
settled, then yielded in one batch — a 60s bash stream showed nothing
for a minute. The turn loop now drains a queue concurrently with
execution: each delta is yielded the moment it lands.
"""

import asyncio

from microagent.core.tool import ToolRegistry, tool
from microagent.core.types import (
    Message,
    TextDelta,
    ToolProgressDelta,
    ToolResult,
    ToolResultDelta,
    TurnComplete,
)
from microagent.llm.client import StreamDone, Usage
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner

from .fake_llm import FakeLLMClient, ScriptedResponse


def _streaming_tool():
    @tool("slow_stream", description="Yields progress slowly.")
    async def slow_stream():
        for i in range(3):
            await asyncio.sleep(0.05)
            yield f"chunk{i}"

    return slow_stream


def _llm():
    return FakeLLMClient(
        [
            ScriptedResponse(
                events=[
                    __import__("microagent.core.types", fromlist=["ToolCallDelta"]).ToolCallDelta(
                        id="c1", name="slow_stream", arguments={}
                    ),
                    Usage(input_tokens=5, output_tokens=5),
                    StreamDone(usage=Usage(input_tokens=5, output_tokens=5), stop_reason="tool_calls"),
                ]
            ),
            ScriptedResponse(
                events=[
                    TextDelta(text="finished", kind="content"),
                    Usage(input_tokens=5, output_tokens=5),
                    StreamDone(usage=Usage(input_tokens=5, output_tokens=5), stop_reason="stop"),
                ]
            ),
        ]
    )


class TestProgressStreaming:
    async def test_progress_arrives_during_execution(self):
        """Each delta must be yielded while the tool is still running —
        i.e. BEFORE the final ToolResultDelta — not batched afterwards."""
        registry = ToolRegistry([_streaming_tool()])
        runner = SessionRunner(llm=_llm(), registry=registry, budget=Budget())

        events = []
        tool_done_at: dict[str, float] = {}
        async for ev in runner.run_turn([Message.user("go")]):
            events.append(ev)
            if isinstance(ev, ToolProgressDelta):
                # While chunk N is being yielded, the tool has NOT yet
                # produced its final result — prove streaming by checking
                # no ToolResultDelta for the call exists yet.
                assert not any(
                    isinstance(e, ToolResultDelta) and e.id == ev.id for e in events
                ), "progress delta arrived after the tool result — batched, not streamed"
            if isinstance(ev, ToolResultDelta):
                tool_done_at[ev.id] = asyncio.get_event_loop().time()

        progress = [e for e in events if isinstance(e, ToolProgressDelta)]
        assert [p.text for p in progress] == ["chunk0", "chunk1", "chunk2"]
        assert any(isinstance(e, TurnComplete) for e in events)
        await runner.close()

    async def test_progress_order_preserved_across_batches(self):
        """Two streaming tools: each tool's own chunks stay in order."""
        def _make(name: str):
            @tool(name, description="stream")
            async def st():
                for i in range(3):
                    await asyncio.sleep(0.02)
                    yield f"{name}-{i}"

            return st

        ToolCallDelta = __import__("microagent.core.types", fromlist=["ToolCallDelta"]).ToolCallDelta
        registry = ToolRegistry([_make("t1"), _make("t2")])
        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    events=[
                        ToolCallDelta(id="a", name="t1", arguments={}),
                        ToolCallDelta(id="b", name="t2", arguments={}),
                        Usage(input_tokens=5, output_tokens=5),
                        StreamDone(usage=Usage(input_tokens=5, output_tokens=5), stop_reason="tool_calls"),
                    ]
                ),
                ScriptedResponse(
                    events=[
                        TextDelta(text="done", kind="content"),
                        Usage(input_tokens=5, output_tokens=5),
                        StreamDone(usage=Usage(input_tokens=5, output_tokens=5), stop_reason="stop"),
                    ]
                ),
            ]
        )
        runner = SessionRunner(llm=llm, registry=registry, budget=Budget())
        events = [e async for e in runner.run_turn([Message.user("go")])]
        await runner.close()

        t1 = [e.text for e in events if isinstance(e, ToolProgressDelta) and e.id == "a"]
        t2 = [e.text for e in events if isinstance(e, ToolProgressDelta) and e.id == "b"]
        assert t1 == ["t1-0", "t1-1", "t1-2"]
        assert t2 == ["t2-0", "t2-1", "t2-2"]

    async def test_interrupt_during_streaming_tool(self):
        """Esc-interrupt while a slow streaming tool runs: the detached
        run task must be cancelled, orphan guards persist, TurnFailed
        surfaces (not a hang)."""
        ToolCallDelta = __import__("microagent.core.types", fromlist=["ToolCallDelta"]).ToolCallDelta

        @tool("endless", description="streams forever")
        async def endless():
            while True:
                await asyncio.sleep(0.02)
                yield "tick"

        llm = FakeLLMClient(
            [
                ScriptedResponse(
                    events=[
                        ToolCallDelta(id="c1", name="endless", arguments={}),
                        Usage(input_tokens=5, output_tokens=5),
                        StreamDone(usage=Usage(input_tokens=5, output_tokens=5), stop_reason="tool_calls"),
                    ]
                ),
            ]
        )
        runner = SessionRunner(llm=llm, registry=ToolRegistry([endless]), budget=Budget())

        async def scenario():
            seen_failed = False
            async for ev in runner.run_turn([Message.user("go")]):
                if isinstance(ev, ToolProgressDelta):
                    runner.interrupt()
                if type(ev).__name__ == "TurnFailed":
                    seen_failed = True
                    break
            return seen_failed

        assert await asyncio.wait_for(scenario(), timeout=10)
        await asyncio.wait_for(runner.close(), timeout=5)

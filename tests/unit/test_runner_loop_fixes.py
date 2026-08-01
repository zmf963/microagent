"""Regression tests for SessionRunner loop-correctness fixes (Phase 2).

Covers:
  2.1 anti-jitter reset per-turn (not per-iteration)
  2.2 steer must not duplicate tool messages in the store
  2.3 truncation must not persist orphaned tool_calls
  2.4 _current_store / _current_loader set per-task in _settle
  2.5 count_tokens includes tool_calls overhead
  2.6 Agent.close() closes the store
"""

import asyncio
import pytest

from microagent.core.types import (
    Message, ToolCall, Usage, TextDelta, ToolCallDelta,
)
from microagent.core.store import InMemoryStore
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.session.runner import SessionRunner
from microagent.session.budget import Budget
from microagent.session.compress import count_tokens
from microagent.llm.client import StreamDone
from tests.unit.fake_llm import FakeLLMClient, text_response, tool_response


# --- 2.1 anti-jitter -------------------------------------------------------

@pytest.mark.asyncio
async def test_anti_jitter_reset_is_per_turn_not_per_iteration():
    """reset_for_new_turn must run once per run_turn(), not inside the while
    loop. Otherwise the ineffective-compression counter can never reach the
    skip threshold, and every iteration retries a provably-ineffective
    compression (burning LLM tokens)."""
    import inspect
    src = inspect.getsource(SessionRunner.run_turn)
    # The reset must appear BEFORE the while loop, not inside it.
    while_idx = src.index("while not self.budget.exhausted")
    reset_idx = src.index("reset_for_new_turn")
    assert reset_idx < while_idx, (
        "reset_for_new_turn must be before the while loop (per-turn), "
        "not inside it (per-iteration)"
    )
    # And it must NOT appear again after the while loop body starts
    after_while = src[while_idx:]
    assert "reset_for_new_turn" not in after_while, (
        "reset_for_new_turn must not appear inside the while loop body"
    )


# --- 2.2 steer must not duplicate tool messages ----------------------------

@pytest.mark.asyncio
async def test_steer_does_not_duplicate_tool_message_in_store():
    """steer modifies the in-memory tool message but must NOT append a
    second tool message to the store — that would create a duplicate
    tool_call_id and the OpenAI API rejects it on resume."""
    store = InMemoryStore()
    # Script: LLM calls a tool, we steer, then LLM responds with text.
    fake = FakeLLMClient([
        tool_response([("c1", "bash", {"command": "echo hi"})]),
        text_response("done"),
    ])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()),
        budget=Budget.root(), store=store, session_id="s1",
    )
    msgs = [Message.user("run echo")]
    # Arm a steer to fire after the first tool result
    runner._steer_pending = "focus on the output"
    async for _ in runner.run_turn(msgs):
        pass

    history = await store.load_history("s1")
    # Count tool messages — must not have duplicates with the same tool_call_id
    tool_msgs = [m for m in history if m.role == "tool"]
    tool_call_ids = [m.tool_call_id for m in tool_msgs]
    assert len(tool_call_ids) == len(set(tool_call_ids)), (
        f"duplicate tool_call_id in store: {tool_call_ids}"
    )
    await runner.close()


# --- 2.3 truncation must not persist orphaned tool_calls -------------------

@pytest.mark.asyncio
async def test_truncation_strips_tool_calls():
    """When the LLM response is truncated (stop_reason=length) with partial
    content AND partial tool_calls, the persisted assistant message must
    NOT include tool_calls — they'd be orphaned (never executed), and the
    next turn fails with 'messages must contain tool results for all tool
    calls'."""
    from microagent.llm.client import StreamEvent

    # A fake LLM that streams partial content + a tool_call, then signals
    # truncation via stop_reason="length" and stream_done.
    class TruncatingLLM:
        def __init__(self):
            self.config = type("C", (), {"base_url": "x", "api_key": "y", "model": "z", "auxiliary_model": None})()

        async def stream(self, **kwargs):
            yield TextDelta(text="partial response...")
            yield ToolCallDelta(id="t1", name="bash", arguments={"command": "echo"})
            yield Usage(input_tokens=10, output_tokens=5)
            yield StreamDone(usage=Usage(input_tokens=10, output_tokens=5), stop_reason="length")

        async def close(self):
            pass

    store = InMemoryStore()
    runner = SessionRunner(
        llm=TruncatingLLM(), registry=ToolRegistry(_default_builtins()),
        budget=Budget.root(), store=store, session_id="s1",
    )
    msgs = [Message.user("test")]
    events = []
    async for ev in runner.run_turn(msgs):
        events.append(ev)

    # The persisted assistant message must have NO tool_calls
    history = await store.load_history("s1")
    assistant_msgs = [m for m in history if m.role == "assistant"]
    assert len(assistant_msgs) >= 1
    last_asst = assistant_msgs[-1]
    assert not last_asst.tool_calls, (
        f"truncated assistant message persisted tool_calls: {last_asst.tool_calls} "
        "— these are orphaned and will be rejected by the API on the next turn"
    )
    await runner.close()


# --- 2.4 _current_store / _current_loader per-task -------------------------

@pytest.mark.asyncio
async def test_settle_sets_current_store_per_task():
    """_settle must re-bind _current_store (and _current_loader) so concurrent
    sessions don't cross-contaminate. Structural check: both must appear in
    _settle's source."""
    import inspect
    src = inspect.getsource(SessionRunner._run_tool_calls)
    assert "_current_store.set" in src, (
        "_settle must set _current_store per-task (was only set in __init__, "
        "causing session_search to read the wrong store under concurrent sessions)"
    )
    assert "_current_managers.set" in src, "must also set MCP managers per-task"


# --- 2.5 count_tokens includes tool_calls ----------------------------------

def test_count_tokens_includes_tool_calls():
    """An assistant message with tool_calls but empty content must count
    as non-zero tokens — the serialized tool_calls represent real API tokens."""
    msg_no_tools = Message.assistant(text="hello world")
    msg_with_tools = Message.assistant(
        text="",
        tool_calls=(ToolCall(id="c1", name="bash", arguments={"command": "echo test"}),),
    )
    no_tools = count_tokens((msg_no_tools,))
    with_tools = count_tokens((msg_with_tools,))
    assert with_tools > no_tools, (
        f"count_tokens must count tool_calls: no_tools={no_tools}, with_tools={with_tools}"
    )
    assert with_tools > 4, f"tool_calls must add tokens: got {with_tools}"


def test_count_tokens_empty_messages():
    assert count_tokens(()) == 0


# --- 2.6 Agent.close() closes the store ------------------------------------

@pytest.mark.asyncio
async def test_agent_close_closes_store(tmp_path):
    """Agent.close() must close the SQLiteStore so connections don't leak
    for library users who manage their own Agent lifecycle."""
    from microagent.agent import Agent
    from microagent.core.store import SQLiteStore
    from microagent.llm.client import LLMConfig
    from tests.unit.fake_llm import FakeLLMClient, text_response

    fake = FakeLLMClient([text_response("ok")])
    store = SQLiteStore(tmp_path / "test.db")
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()),
        budget=Budget.root(), store=store, session_id="s1",
    )
    from microagent.agent import Agent
    agent = Agent(runner=runner, registry=runner.registry)

    await agent.close()
    # SQLiteStore.close() sets conn to closed state. Verify by attempting
    # a query — it should raise (cannot operate on closed database).
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1").fetchone()

"""Regression tests for bugs found via crazy/stress testing.

5 bugs found through 36 adversarial inline probes against the real code:
1. compress.py BudgetExceeded NameError (circuit breaker dead code)
2. Budget.consume accepting negative values (budget expansion)
3. ToolOutputStore string base_dir crash
4. Stale _steer_pending leaking across turns
5. get_pricing(None) crash
"""

import asyncio
import pytest
import tempfile

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, TurnFailed, Usage
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from tests.unit.fake_llm import (
    FakeLLMClient, text_response, tool_response, ScriptedResponse,
)


# === Bug 1: compress.py BudgetExceeded NameError ===========================

@pytest.mark.asyncio
async def test_compress_llm_error_triggers_circuit_breaker_not_nameerror():
    """When the LLM raises a non-BudgetExceeded error during compression,
    the except BudgetExceeded clause used to raise NameError (the name was
    never imported at module scope), so the circuit-breaker's record_failure()
    never ran — every turn silently retried compression with no backoff."""
    from microagent.session.compress import compact_conversation, CompactionState

    class ErrorLLM:
        config = type("C", (), {
            "base_url": "x", "api_key": "y", "model": "z",
            "auxiliary_model": None,
        })()

        async def stream(self, **kwargs):
            raise RuntimeError("compress LLM blew up")
            yield  # make it an async generator

        async def close(self):
            pass

    msgs = tuple(Message.user(f"q{i}") for i in range(20))
    state = CompactionState()
    # Before the fix: this raised NameError (BudgetExceeded not in scope)
    # After the fix: the except Exception fires, record_failure runs, returns _fallback
    result = await compact_conversation(msgs, ErrorLLM(), state=state, force=True)
    assert len(result) >= 1  # got the fallback, not a crash
    # Circuit breaker recorded the failure
    assert state.consecutive_failures >= 1, (
        f"circuit breaker should have recorded failure, got {state.consecutive_failures}"
    )


@pytest.mark.asyncio
async def test_compress_budget_exceeded_propagates_cleanly():
    """A real BudgetExceeded during compression must propagate (not be
    swallowed by a misfired NameError handler)."""
    from microagent.session.compress import compact_conversation, CompactionState
    from microagent.session.budget import Budget, BudgetExceeded

    class BudgetExceededLLM:
        config = type("C", (), {
            "base_url": "x", "api_key": "y", "model": "z",
            "auxiliary_model": None,
        })()

        async def stream(self, **kwargs):
            # Simulate consuming budget past the limit mid-stream
            raise BudgetExceeded("budget blown during compaction")
            yield

        async def close(self):
            pass

    msgs = tuple(Message.user(f"q{i}") for i in range(20))
    state = CompactionState()
    budget = Budget.root(max_cost_usd=0.0001)
    with pytest.raises(BudgetExceeded):
        await compact_conversation(
            msgs, BudgetExceededLLM(), state=state, budget=budget, force=True,
        )


# === Bug 2: Budget.consume negative leak ==================================

@pytest.mark.asyncio
async def test_budget_consume_clamps_negative_tokens():
    """Negative token/cost consumption used to DEC the used_* counters,
    making remaining_cost EXPAND beyond the original limit ($5 → $7).
    A malicious caller could expand the budget 3× via negative consume."""
    b = Budget.root(max_iterations=10, max_tokens=1000, max_cost_usd=5.0)
    await b.consume(tokens=-500, cost_usd=-2.0, iterations=-3)
    assert b._used_tokens == 0, f"negative tokens leaked: {b._used_tokens}"
    assert b._used_cost == 0.0, f"negative cost leaked: {b._used_cost}"
    assert b._used_iter == 0, f"negative iter leaked: {b._used_iter}"
    # remaining_cost stays at the original limit, not expanded
    assert b.remaining_cost == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_budget_consume_usage_clamps_negative():
    """consume_usage delegates to consume; verify the clamp covers that path
    too (Usage with negative fields from proxy-cached-token adjustments)."""
    b = Budget.root(max_cost_usd=5.0)
    await b.consume_usage(Usage(input_tokens=-1000, output_tokens=-500, cost_usd=-0.5))
    assert b._used_cost == 0.0, f"negative cost via consume_usage: {b._used_cost}"


# === Bug 3: ToolOutputStore string base_dir ===============================

def test_output_store_accepts_string_base_dir():
    """Constructing ToolOutputStore with base_dir='/path' (a string, the
    natural Python idiom) used to crash with TypeError on the first path
    join (str / str). The annotation said Path | None but didn't convert."""
    from microagent.tools.output_store import ToolOutputStore
    tmp = tempfile.mkdtemp()
    # String path — must not crash
    store = ToolOutputStore(base_dir=tmp)
    big = "x" * (store.max_bytes + 100)
    out = store.process("call-1", big, session_id="sess-1")
    assert out.saved_to_disk
    assert out.disk_path is not None


def test_output_store_accepts_path_base_dir():
    """Path base_dir (the only previously-tested form) still works."""
    from pathlib import Path
    from microagent.tools.output_store import ToolOutputStore
    tmp = Path(tempfile.mkdtemp())
    store = ToolOutputStore(base_dir=tmp)
    big = "x" * (store.max_bytes + 100)
    out = store.process("call-2", big, session_id="sess-2")
    assert out.saved_to_disk


# === Bug 4: stale _steer_pending leak =====================================

@pytest.mark.asyncio
async def test_steer_pending_persists_across_text_turn():
    """A steer set before a pure-text turn must persist to the next turn.

    Per the documented contract (steer() docstring + test_steer_pure_text_
    response_waits): 'If the current iteration has no tool calls (pure text
    response), the steer waits until the next turn.' A text-only turn can't
    consume a steer (it needs a tool_result), so _steer_pending must remain
    set. An earlier attempt to clear it at run_turn entry (commit a438b7f)
    broke this contract and was reverted."""
    runner = SessionRunner(
        llm=FakeLLMClient([text_response("ok"), text_response("ok")]),
        registry=ToolRegistry(_default_builtins()),
        budget=Budget.root(max_iterations=2),
    )
    await runner.steer("pending steer")
    assert runner._steer_pending == "pending steer"
    # Text-only turn — no tool calls, so the steer can't be consumed
    msgs = [Message.user("hi")]
    async for _ in runner.run_turn(msgs):
        pass
    # The steer must persist (documented wait-for-next-turn contract)
    assert runner._steer_pending == "pending steer", (
        "steer must persist across a pure-text turn (documented contract)"
    )
    await runner.close()


@pytest.mark.asyncio
async def test_steer_pending_consumed_by_tool_turn():
    """A pending steer IS consumed when a turn runs tool calls."""
    from microagent.core.types import ToolResultDelta
    runner = SessionRunner(
        llm=FakeLLMClient([
            tool_response([("c1", "bash", {"command": "echo hi"})]),
            text_response("done"),
        ]),
        registry=ToolRegistry(_default_builtins()),
        budget=Budget.root(),
    )
    await runner.steer("consume me")
    msgs = [Message.user("run echo")]
    async for ev in runner.run_turn(msgs):
        pass
    # After a tool-turn, the steer was consumed
    assert runner._steer_pending is None, (
        f"steer should be consumed by a tool turn, still {runner._steer_pending!r}"
    )
    await runner.close()


# === Bug 5: pricing None model ============================================

def test_get_pricing_none_returns_fallback():
    """get_pricing(None) used to crash with AttributeError on None.lower().
    Should return the conservative fallback price instead."""
    from microagent.llm.pricing import get_pricing
    assert get_pricing(None) == (0.50, 0.50)


def test_get_context_window_none_returns_fallback():
    from microagent.llm.pricing import get_context_window
    assert get_context_window(None) == 128_000


def test_get_pricing_empty_string_returns_fallback():
    from microagent.llm.pricing import get_pricing
    assert get_pricing("") == (0.50, 0.50)

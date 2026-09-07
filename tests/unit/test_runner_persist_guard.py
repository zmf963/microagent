"""Round-22 🔴 regression: the tool-result persist loop must not leave
orphaned tool_calls in the store when a mid-loop I/O error hits.

The assistant message (with tool_calls) is persisted BEFORE results are
settled. If output-store processing or store.append raised mid-loop, the
raw exception escaped run_turn and the store kept tool_calls with only a
partial set of results — the OpenAI API then rejects the resumed session
("messages must contain tool results for all tool calls"). The fix
persists an error result for the failing and every remaining call, then
yields TurnFailed(code="store_error").
"""

from microagent.core.store import InMemoryStore
from microagent.core.tool import ToolRegistry, tool
from microagent.core.types import Message, ToolResult, TurnFailed
from microagent.session.runner import SessionRunner

from .fake_llm import FakeLLMClient, ScriptedResponse, text_response, tool_response


@tool("echo_ok", description="Return a fixed ok result.")
async def echo_ok() -> ToolResult:
    return ToolResult.ok("ok")


class FlakyStore(InMemoryStore):
    """Fails append() on the Nth tool_result message (1-based)."""

    def __init__(self, fail_on: int):
        super().__init__()
        self._fail_on = fail_on
        self._tool_result_count = 0

    async def append(self, session_id: str, msg: Message):
        if msg.role == "tool_result" or getattr(msg, "tool_call_id", None):
            self._tool_result_count += 1
            if self._tool_result_count == self._fail_on:
                raise OSError("simulated disk full")
        await super().append(session_id, msg)


async def _collect(runner, messages):
    events = []
    async for ev in runner.run_turn(messages):
        events.append(ev)
    return events


class TestPersistLoopGuard:
    async def test_midloop_persist_failure_leaves_no_orphans(self):
        """2 tool calls; the 1st result persist fails. Both calls must end
        up with persisted error results and the turn must fail cleanly."""
        llm = FakeLLMClient(
            [
                tool_response(
                    [
                        ("c1", "echo_ok", {}),
                        ("c2", "echo_ok", {}),
                    ]
                ),
                text_response("done"),
            ]
        )
        store = FlakyStore(fail_on=1)
        registry = ToolRegistry()
        registry.register(echo_ok)
        runner = SessionRunner(llm=llm, registry=registry, store=store)

        events = await _collect(runner, [Message.user("hi")])

        # Turn fails with the store_error code instead of raising.
        failures = [e for e in events if isinstance(e, TurnFailed)]
        assert failures and failures[0].code == "store_error"

        # Store invariant: every persisted assistant tool_call has a
        # matching persisted tool_result.
        history = await store.load_history("default")
        call_ids: list[str] = []
        result_ids: list[str] = []
        for m in history:
            for tc in getattr(m, "tool_calls", None) or []:
                call_ids.append(tc.id)
            tcid = getattr(m, "tool_call_id", None)
            if tcid:
                result_ids.append(tcid)
        assert sorted(call_ids) == ["c1", "c2"]
        assert sorted(result_ids) == ["c1", "c2"], (
            "orphaned tool_calls in store — resumed session would be "
            f"rejected by the API (calls={call_ids}, results={result_ids})"
        )

    async def test_second_result_persist_failure_covers_remaining(self):
        """Failure on the 2nd of 3 calls: calls 2 and 3 both get error
        results persisted; call 1 keeps its real result."""
        llm = FakeLLMClient(
            [
                tool_response(
                    [
                        ("c1", "echo_ok", {}),
                        ("c2", "echo_ok", {}),
                        ("c3", "echo_ok", {}),
                    ]
                ),
                text_response("done"),
            ]
        )
        store = FlakyStore(fail_on=2)
        registry = ToolRegistry()
        registry.register(echo_ok)
        runner = SessionRunner(llm=llm, registry=registry, store=store)

        events = await _collect(runner, [Message.user("hi")])

        failures = [e for e in events if isinstance(e, TurnFailed)]
        assert failures and failures[0].code == "store_error"
        history = await store.load_history("default")
        call_ids = sorted(
            tc.id for m in history for tc in (getattr(m, "tool_calls", None) or [])
        )
        result_ids = sorted(
            tcid
            for m in history
            if (tcid := getattr(m, "tool_call_id", None))
        )
        assert call_ids == ["c1", "c2", "c3"]
        assert result_ids == ["c1", "c2", "c3"]

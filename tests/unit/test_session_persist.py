"""Tests for session persistence — auto-checkpoint and resume."""

from microagent import InMemoryStore, Message, SessionRunner, SQLiteStore, ToolRegistry
from microagent.core.types import TurnComplete
from tests.unit.fake_llm import FakeLLMClient, text_response


class TestSessionPersistence:
    async def test_auto_append_on_turn_complete(self):
        """SessionRunner with store auto-saves messages on TurnComplete."""
        store = InMemoryStore()
        llm = FakeLLMClient([text_response("hello")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry(), store=store)

        messages = [Message.user("hi")]
        async for _ in runner.run_turn(messages):
            pass

        history = await store.load_history("default")
        assert len(history) >= 2  # user + assistant
        assert history[0].content == "hi"
        assert history[-1].content == "hello"

    async def test_sqlite_auto_save(self, tmp_path):
        """SQLiteStore auto-saves and checkpoints."""
        store = SQLiteStore(tmp_path / "auto.db")
        llm = FakeLLMClient([text_response("persisted")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry(), store=store)

        messages = [Message.user("save me")]
        async for _ in runner.run_turn(messages):
            pass

        # Close and reopen — data persists
        store.close()
        store2 = SQLiteStore(tmp_path / "auto.db")
        history = await store2.load_history("default")
        assert len(history) >= 2
        assert "persisted" in history[-1].content
        store2.close()

    async def test_resume_and_continue(self, tmp_path):
        """Resume a previous session and continue."""
        store = SQLiteStore(tmp_path / "resume.db")

        # Turn 1: save some messages
        llm1 = FakeLLMClient([text_response("I am an AI.")])
        runner1 = SessionRunner(llm=llm1, registry=ToolRegistry(), store=store)
        async for _ in runner1.run_turn([Message.user("who are you?")]):
            pass

        # Turn 2: resume and continue
        llm2 = FakeLLMClient([text_response("I remember you asked who I am.")])
        runner2 = SessionRunner(llm=llm2, registry=ToolRegistry(), store=store)
        history = await runner2.resume("default", store)
        assert len(history) >= 2

        messages = list(history) + [Message.user("what did I ask?")]
        result = None
        async for event in runner2.run_turn(messages):
            if isinstance(event, TurnComplete):
                result = event.content
        assert "remember" in result

        store.close()

    async def test_no_duplicate_user_on_retry_same_list(self):
        """A turn that fails (budget exhausted) followed by a retry with the
        SAME messages list must not persist the trailing user message twice."""
        from microagent.session.budget import Budget

        store = InMemoryStore()
        llm = FakeLLMClient([text_response("reply")])
        runner = SessionRunner(
            llm=llm, registry=ToolRegistry(), store=store,
            budget=Budget(max_iterations=0),  # turn fails immediately
        )
        messages = [Message.user("same question")]
        async for _ in runner.run_turn(messages):
            pass
        # Retry after raising the budget — same list object
        runner2 = SessionRunner(
            llm=FakeLLMClient([text_response("reply")]),
            registry=ToolRegistry(), store=store,
        )
        async for _ in runner2.run_turn(messages):
            pass
        history = await store.load_history("default")
        users = [m for m in history if m.role == "user" and m.content == "same question"]
        assert len(users) == 1

    async def test_no_duplicate_user_on_resume_unanswered(self):
        """Resume a session whose tail is an unanswered user message (crash
        before the assistant reply): passing the loaded history back must
        not re-persist that message."""
        store = InMemoryStore()
        llm = FakeLLMClient([text_response("late answer")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry(), store=store)
        await store.append("default", Message.user("unanswered q"))
        history = await runner.resume("default", store)
        messages = list(history)
        async for _ in runner.run_turn(messages):
            pass
        final = await store.load_history("default")
        users = [m for m in final if m.role == "user" and m.content == "unanswered q"]
        assert len(users) == 1

    async def test_identical_consecutive_user_messages_both_persisted(self):
        """Dedupe must not eat legit repeats: after a completed turn the
        store tail is the assistant message, so an identical follow-up IS
        appended."""
        store = InMemoryStore()
        llm = FakeLLMClient([text_response("a1"), text_response("a2")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry(), store=store)
        messages = [Message.user("继续")]
        async for _ in runner.run_turn(messages):
            pass
        messages.append(Message.assistant("a1"))
        messages.append(Message.user("继续"))
        async for _ in runner.run_turn(messages):
            pass
        history = await store.load_history("default")
        users = [m for m in history if m.role == "user" and m.content == "继续"]
        assert len(users) == 2

    async def test_turn_pins_session_id_against_mid_turn_swap(self):
        """The cron scheduler swaps runner.session_id around its own arun.
        An in-flight turn must keep writing to the session it STARTED in —
        store appends, output-store paths and turn_complete all use the
        pinned id captured under the turn lock."""
        from microagent.core.tool import tool as tool_decorator
        from microagent.core.types import ToolResult
        from tests.unit.fake_llm import tool_response

        store = InMemoryStore()
        llm = FakeLLMClient([
            tool_response([("tc1", "swapper", {})]),
            text_response("final answer"),
        ])

        @tool_decorator("swapper", description="swaps session id mid-turn")
        async def _swapper() -> ToolResult:
            runner.session_id = "cron-job"  # simulate cron tick interleaving
            return ToolResult.ok("swapped")

        runner = SessionRunner(llm=llm, registry=ToolRegistry([_swapper]), store=store)
        messages = [Message.user("go")]
        async for _ in runner.run_turn(messages):
            pass

        default_hist = await store.load_history("default")
        cron_hist = await store.load_history("cron-job")
        assert any(m.content == "final answer" for m in default_hist)
        assert cron_hist == []

    async def test_store_is_optional(self):
        """Store=None should work without errors (backward compat)."""
        llm = FakeLLMClient([text_response("ok")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry(), store=None)
        messages = [Message.user("hi")]
        async for _ in runner.run_turn(messages):
            pass
        # Should not crash

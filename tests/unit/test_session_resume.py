"""Tests for session resume functionality."""

from microagent import InMemoryStore, Message, SessionRunner, SQLiteStore, ToolRegistry
from microagent.core.types import TurnComplete
from tests.unit.fake_llm import FakeLLMClient, text_response


class TestSessionResume:
    async def test_resume_in_memory(self):
        """Resume from InMemoryStore restores conversation history."""
        store = InMemoryStore()
        # Save a previous conversation
        await store.append("s1", Message.user("what is Python?"))
        await store.append("s1", Message.assistant("Python is a programming language."))

        # Resume
        llm = FakeLLMClient([text_response("Python was created by Guido.")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry())
        history = await runner.resume("s1", store)
        assert len(history) == 2
        assert history[0].content == "what is Python?"

    async def test_resume_sqlite(self, tmp_path):
        """Resume from SQLiteStore restores persisted messages."""
        store = SQLiteStore(tmp_path / "test.db")
        await store.append("s1", Message.user("hello"))
        await store.append("s1", Message.assistant("hi there"))
        await store.checkpoint("s1")

        runner = SessionRunner(llm=FakeLLMClient([text_response("done")]), registry=ToolRegistry())
        history = await runner.resume("s1", store)
        assert len(history) == 2
        assert history[0].content == "hello"
        assert history[1].content == "hi there"
        store.close()

    async def test_resume_nonexistent_session(self):
        """Resume a nonexistent session returns empty tuple."""
        store = InMemoryStore()
        runner = SessionRunner(llm=FakeLLMClient([text_response("ok")]), registry=ToolRegistry())
        history = await runner.resume("nonexistent", store)
        assert history == ()

    async def test_resume_and_continue(self):
        """Resume and continue conversation: old history preserved + new turn."""
        store = InMemoryStore()
        await store.append("s1", Message.user("who are you?"))
        await store.append("s1", Message.assistant("I am an AI."))

        llm = FakeLLMClient([text_response("I'm still here.")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry())
        history = await runner.resume("s1", store)

        # Continue: pass history + new message
        messages = list(history) + [Message.user("are you still there?")]
        result = None
        async for ev in runner.run_turn(messages):
            if isinstance(ev, TurnComplete):
                result = ev.content
        assert result == "I'm still here."

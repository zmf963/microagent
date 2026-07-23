"""Tests for session persistence — auto-checkpoint and resume."""

import pytest
from pathlib import Path
from microagent import SessionRunner, ToolRegistry, InMemoryStore, SQLiteStore, Message
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

    async def test_store_is_optional(self):
        """Store=None should work without errors (backward compat)."""
        llm = FakeLLMClient([text_response("ok")])
        runner = SessionRunner(llm=llm, registry=ToolRegistry(), store=None)
        messages = [Message.user("hi")]
        async for _ in runner.run_turn(messages):
            pass
        # Should not crash

"""Tests for role alternation check in compact_conversation output.

Some APIs (e.g. certain OpenRouter routes) require strict user/assistant
alternation. After compaction, adjacent same-role messages are fixed by
inserting empty user messages.
"""

from microagent.core.types import Message
from microagent.session.compress import (
    CompactionState,
    _ensure_role_alternation,
    compact_conversation,
)
from tests.unit.fake_llm import FakeLLMClient, text_response


class TestRoleAlternation:
    def test_no_adjacent_same_role_passes_through(self):
        """Already alternating messages are unchanged."""
        messages = (
            Message.user("hello"),
            Message.assistant("hi"),
            Message.user("do something"),
            Message.assistant("done"),
        )
        result = _ensure_role_alternation(messages, strict=False)
        assert result == messages

    def test_adjacent_assistant_inserts_empty_user(self):
        """Two consecutive assistant messages → empty user inserted between."""
        messages = (
            Message.user("hello"),
            Message.assistant("first"),
            Message.assistant("second"),
        )
        result = _ensure_role_alternation(messages, strict=True)
        assert len(result) == 4
        assert result[1].role == "assistant"
        assert result[2].role == "user"
        assert result[2].content == ""
        assert result[3].role == "assistant"

    def test_adjacent_user_inserts_empty_assistant(self):
        """Two consecutive user messages → empty assistant inserted between."""
        messages = (
            Message.user("first"),
            Message.user("second"),
        )
        result = _ensure_role_alternation(messages, strict=True)
        assert len(result) == 3
        assert result[0].role == "user"
        assert result[1].role == "assistant"
        assert result[1].content == ""
        assert result[2].role == "user"

    def test_strict_disabled_by_default(self):
        """When strict=False (default), no alternation is enforced."""
        messages = (
            Message.user("first"),
            Message.user("second"),
        )
        result = _ensure_role_alternation(messages, strict=False)
        assert result == messages  # unchanged

    async def test_compact_with_strict_alternation(self):
        """compact_conversation with strict_role_alternation=True fixes output."""
        # Build messages that would produce adjacent same roles after LLM summary
        llm = FakeLLMClient([text_response("summary text")])
        messages = tuple([Message.user(f"msg {i}") for i in range(20)])
        state = CompactionState()
        result = await compact_conversation(
            messages,
            llm=llm,
            context_window=100,  # force LLM summary
            state=state,
            force=True,
            strict_role_alternation=True,
        )
        # Check no adjacent same roles (excluding tool messages)
        non_tool = [m for m in result if m.role != "tool"]
        for i in range(1, len(non_tool)):
            if non_tool[i].role == non_tool[i - 1].role:
                # Only allowed if one of them is the empty separator
                assert non_tool[i].content == "" or non_tool[i - 1].content == ""

    async def test_compact_without_strict_no_change(self):
        """compact_conversation with strict_role_alternation=False (default)."""
        llm = FakeLLMClient([text_response("summary text")])
        messages = tuple([Message.user(f"msg {i}") for i in range(20)])
        state = CompactionState()
        result = await compact_conversation(
            messages,
            llm=llm,
            context_window=100,
            state=state,
            force=True,
            strict_role_alternation=False,
        )
        # No crashes — alternation not enforced
        assert len(result) >= 1

"""Tests for incremental compaction — append to existing summary."""

import pytest
from microagent.core.types import Message, ToolResult
from microagent.session.compress import (
    micro_compact,
    snip_tool_results,
    build_compaction_summary_prompt,
    build_incremental_summary_prompt,
    CompactionState,
    compact_conversation,
    estimate_tokens,
    count_tokens,
)


class TestIncrementalSummary:
    def test_prompt_includes_previous_summary(self):
        """Incremental prompt includes the previous summary text."""
        messages = (
            Message.user("msg1"),
            Message.assistant("reply1"),
        )
        previous = "## Previous Summary\nUser asked about auth."
        prompt = build_incremental_summary_prompt(messages, previous_summary=previous)
        assert "Previous Summary" in prompt
        assert "msg1" in prompt
        assert "auth" in prompt

    def test_prompt_focuses_on_new_content(self):
        """Incremental prompt specifies it's updating, not creating from scratch."""
        messages = (Message.user("msg1"),)
        prompt = build_incremental_summary_prompt(messages, previous_summary="old")
        assert "update" in prompt.lower() or "existing" in prompt.lower() or "previous" in prompt.lower()

    def test_state_stores_previous_summary(self):
        """CompactionState stores and retrieves previous summary."""
        state = CompactionState()
        assert state.previous_summary is None

        state.previous_summary = "## Summary\nDid some work."
        assert "Summary" in state.previous_summary

    def test_state_clears_summary_on_fresh_compaction(self):
        """Fresh state has no previous summary."""
        state = CompactionState()
        state.record_success()  # success doesn't clear summary
        assert state.previous_summary is None  # only set by compaction itself

    def test_state_serializable_for_session_persistence(self):
        """State attributes are plain Python types for pickling."""
        state = CompactionState()
        state.previous_summary = "summary text"
        d = {
            "fails": state.consecutive_failures,
            "summary": state.previous_summary,
        }
        assert isinstance(d["fails"], int)
        assert isinstance(d["summary"], str)

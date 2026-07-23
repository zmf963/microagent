"""Tests for the 4-layer compression pyramid."""

import pytest
from microagent.core.types import Message, ToolResult
from microagent.session.compress import (
    micro_compact,
    snip_tool_results,
    build_compaction_summary_prompt,
    CompactionState,
    estimate_tokens,
    count_tokens,
)


class TestMicroCompact:
    def test_truncates_long_tool_results(self):
        """Tool results >500 chars are truncated to a placeholder."""
        messages = (
            Message.user("read app.py"),
            Message.assistant("let me read", tool_calls=()),
            Message.tool_result(ToolResult.ok("x" * 600), tool_call_id="c1"),
            Message.assistant("file contents: " + "x" * 600),
        )
        result = micro_compact(messages)
        # Tool result should be truncated
        assert len(result[2].content) < 600
        assert "truncated" in result[2].content.lower()

    def test_preserves_user_messages(self):
        """User messages are never truncated."""
        messages = (
            Message.user("hello " + "x" * 600),
        )
        result = micro_compact(messages)
        assert len(result[0].content) > 600  # unchanged

    def test_preserves_errors(self):
        """Error tool results are never truncated (is_error=True)."""
        messages = (
            Message.tool_result(ToolResult.error("e" * 600), tool_call_id="c1"),
        )
        result = micro_compact(messages)
        # Error results preserved (not truncated)
        assert "truncated" not in result[0].content.lower()

    def test_no_change_when_under_limit(self):
        """Messages under limit are unchanged."""
        messages = (
            Message.user("hi"),
            Message.assistant("hello"),
        )
        result = micro_compact(messages)
        assert result == messages

    def test_short_tool_results_preserved(self):
        """Short tool results (<500 chars) are preserved."""
        messages = (
            Message.tool_result(ToolResult.ok("short"), tool_call_id="c1"),
        )
        result = micro_compact(messages)
        assert result[0].content == "short"


class TestSnipToolResults:
    def test_removes_oldest_tool_results(self):
        """Oldest tool_result messages are removed first."""
        messages = tuple(
            Message.tool_result(ToolResult.ok(f"result{i}"), tool_call_id=f"c{i}")
            for i in range(20)
        )
        result = snip_tool_results(messages, keep_recent=5, max_tokens=20)
        # Should have removed oldest, kept 5 most recent
        assert len(result) < 20
        # Last result should be preserved
        assert "result19" in result[-1].content

    def test_preserves_non_tool_messages(self):
        """User and assistant messages are preserved during snip."""
        messages = (
            Message.user("hello"),
            Message.tool_result(ToolResult.ok("old"), tool_call_id="c1"),
            Message.assistant("response"),
            Message.tool_result(ToolResult.ok("new"), tool_call_id="c2"),
        )
        result = snip_tool_results(messages, keep_recent=0, max_tokens=15)
        # User and assistant must survive (snip only removes tool results)
        roles = [m.role for m in result]
        assert "user" in roles
        assert "assistant" in roles
        # Both tool results survive (under token limit after user/assist removed? no — keep_recent=0 removes oldest tool results)
        # With max_tokens=15 and keep_recent=0, oldest tool results snipped first
        tool_contents = " ".join(m.content for m in result if m.role == "tool")
        assert "new" in tool_contents  # newer tool result kept


class TestCompactionPrompt:
    def test_includes_all_sections(self):
        """Summary prompt includes all 7 required sections."""
        messages = (
            Message.user("fix bug in auth.py"),
            Message.assistant("found the issue"),
        )
        prompt = build_compaction_summary_prompt(messages)
        sections = [
            "主要请求和意图",
            "关键技术决策",
            "涉及的文件和代码",
            "遇到的错误和修复",
            "所有用户消息",
            "待办任务",
            "当前进度",
        ]
        for s in sections:
            assert s in prompt, f"Missing section: {s}"

    def test_includes_all_user_messages(self):
        """All user messages are enumerated in the prompt."""
        messages = (
            Message.user("msg1"),
            Message.assistant("reply1"),
            Message.user("msg2"),
            Message.assistant("reply2"),
            Message.user("msg3"),
        )
        prompt = build_compaction_summary_prompt(messages)
        assert "msg1" in prompt
        assert "msg2" in prompt
        assert "msg3" in prompt


class TestCompactionState:
    def test_initial_state(self):
        state = CompactionState()
        assert state.consecutive_failures == 0
        assert not state.is_cooling_down()

    def test_record_failure(self):
        state = CompactionState()
        state.record_failure()
        state.record_failure()
        assert state.consecutive_failures == 2

    def test_circuit_breaker(self):
        state = CompactionState()
        for _ in range(3):
            state.record_failure()
        assert state.is_circuit_broken()

    def test_record_success_resets(self):
        state = CompactionState()
        state.record_failure()
        state.record_failure()
        state.record_success()
        assert state.consecutive_failures == 0

    def test_cooldown(self):
        state = CompactionState()
        state.activate_cooldown()
        assert state.is_cooling_down()

"""Tests for context compression — long conversation summarization."""

import pytest
from microagent.session.compress import snip_tool_results, estimate_tokens, count_tokens
from microagent.core.types import Message, ToolResult


class TestEstimateTokens:
    def test_estimate_english(self):
        n = estimate_tokens("Hello world. This is a test.")
        assert n > 0
        assert n < 20

    def test_estimate_empty(self):
        assert estimate_tokens("") == 0

    def test_estimate_chinese(self):
        n = estimate_tokens("你好世界这是一个测试")
        # Chinese chars ~2 tokens each
        assert n > 5


class TestCompressHistory:
    def test_no_compression_needed(self):
        """Short history under token limit should not be compressed."""
        messages = (
            Message.user("hi"),
            Message.assistant("hello"),
        )
        result = snip_tool_results(messages, max_tokens=100)
        assert result == messages  # unchanged

    def test_compress_truncates_early_messages(self):
        """When over limit with tool messages, oldest are snipped first."""
        # Mix of user + tool messages
        messages = tuple(
            Message.tool_result(ToolResult.ok(f"result{i}"), tool_call_id=f"c{i}")
            for i in range(50)
        )
        result = snip_tool_results(messages, max_tokens=100, keep_recent=5)
        # Result should be shorter (snip removes oldest tool results)
        assert len(result) < len(messages)
        # Most recent results preserved
        assert "result49" in result[-1].content

    def test_summary_preserves_last_messages(self):
        """The most recent messages should never be truncated."""
        messages = tuple(
            Message.user(f"msg{i}") for i in range(50)
        )
        result = snip_tool_results(messages, max_tokens=100)
        # Last message always preserved
        assert result[-1].content == "msg49"

    def test_compress_keeps_user_assistant_pairs(self):
        """Snip preserves user/assistant messages, removes tool results."""
        messages = (
            Message.user("u1"), Message.assistant("a1"),
            Message.user("u2"), Message.assistant("a2"),
            Message.user("u3"), Message.assistant("a3"),
            Message.user("u4"), Message.assistant("a4"),
        )
        result = snip_tool_results(messages, max_tokens=15)
        # User/assistant messages are preserved
        assert result[-1].role == "assistant"

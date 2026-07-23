"""Tests for context compression — long conversation summarization."""

import pytest
from microagent.session.compress import compress_history, estimate_tokens
from microagent.core.types import Message


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
        result = compress_history(messages, max_tokens=100)
        assert result == messages  # unchanged

    def test_compress_truncates_early_messages(self):
        """When over limit, early messages are truncated/replaced with summary."""
        # Create enough messages to exceed a small limit
        messages = tuple(
            Message.user(f"message number {i} with some extra padding text") 
            for i in range(100)
        )
        result = compress_history(messages, max_tokens=200)
        # Result should be shorter
        assert len(result) < len(messages)
        # First message should be a summary placeholder
        assert "summary" in result[0].content.lower() or "compressed" in result[0].content.lower()
        # Last messages should be preserved
        assert result[-1].content == messages[-1].content

    def test_summary_preserves_last_messages(self):
        """The most recent messages should never be truncated."""
        messages = tuple(
            Message.user(f"msg{i}") for i in range(50)
        )
        result = compress_history(messages, max_tokens=100)
        # Last message always preserved
        assert result[-1].content == "msg49"

    def test_compress_keeps_user_assistant_pairs(self):
        """Compression should not break user/assistant alternation."""
        messages = (
            Message.user("u1"), Message.assistant("a1"),
            Message.user("u2"), Message.assistant("a2"),
            Message.user("u3"), Message.assistant("a3"),
            Message.user("u4"), Message.assistant("a4"),
        )
        result = compress_history(messages, max_tokens=15)
        # Must end with assistant
        assert result[-1].role == "assistant"

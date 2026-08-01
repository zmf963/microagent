"""Tests for token estimation edge cases."""

import pytest

from microagent.core.types import Message
from microagent.session.compress import count_tokens, estimate_tokens


class TestTokenEstimation:
    @pytest.mark.parametrize(
        "text,expected_range",
        [
            ("", (0, 0)),
            ("a", (1, 1)),
            ("hello", (1, 3)),
            ("hello world", (2, 5)),
            ("你好", (1, 3)),
            ("你好世界", (1, 5)),
            ("hello 你好 world", (2, 8)),
            ("\n\n\n", (0, 3)),
            (" " * 10, (1, 5)),
            ("x" * 1000, (200, 300)),
        ],
    )
    def test_estimate_range(self, text, expected_range):
        """Token count falls within a reasonable range."""
        lo, hi = expected_range
        tokens = estimate_tokens(text)
        assert lo <= tokens <= hi, f"{repr(text[:20])}: {tokens} not in [{lo}, {hi}]"

    def test_estimate_non_empty_positive(self):
        """Every non-empty string gets at least 1 token."""
        assert estimate_tokens("a") == 1
        assert estimate_tokens("中") == 1
        assert estimate_tokens(".") == 1

    def test_estimate_monotonic(self):
        """Longer text => more tokens (monotonic)."""
        assert estimate_tokens("aaa") >= estimate_tokens("aa")
        assert estimate_tokens("你好世界") >= estimate_tokens("你好")

    def test_count_tokens_aggregates(self):
        """count_tokens sums across messages (including role framing overhead)."""
        msgs = (
            Message.user("hi"),
            Message.assistant("hello there"),
        )
        total = count_tokens(msgs)
        # "hi" ~1 + "hello there" ~2-3 content + 4 tokens/msg framing overhead
        # (2 msgs × 4 = 8) → ~11-12 total. The framing overhead was added so
        # that assistant messages with tool_calls but empty content still
        # count as non-zero tokens.
        assert 9 <= total <= 16

"""Tests for context compression — long conversation summarization."""

from microagent.core.types import Message, ToolResult
from microagent.session.compress import estimate_tokens, snip_tool_results


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
        messages = tuple(Message.user(f"msg{i}") for i in range(50))
        result = snip_tool_results(messages, max_tokens=100)
        # Last message always preserved
        assert result[-1].content == "msg49"

    def test_compress_keeps_user_assistant_pairs(self):
        """Snip preserves user/assistant messages, removes tool results."""
        messages = (
            Message.user("u1"),
            Message.assistant("a1"),
            Message.user("u2"),
            Message.assistant("a2"),
            Message.user("u3"),
            Message.assistant("a3"),
            Message.user("u4"),
            Message.assistant("a4"),
        )
        result = snip_tool_results(messages, max_tokens=15)
        # User/assistant messages are preserved
        assert result[-1].role == "assistant"


class TestAutoCompressionGate:
    async def test_few_messages_over_threshold_still_compacts(self):
        """The auto-compression gate was `len(messages) > 10` — a 3-message
        conversation with huge content never compacted no matter how far
        over the token threshold it went. The gate must be token-based."""
        from microagent.core.tool import ToolRegistry
        from microagent.session.budget import Budget
        from microagent.session.runner import SessionRunner
        from tests.unit.fake_llm import FakeLLMClient, text_response

        # 3 messages, ~30K chars each ≈ way over a 100-token threshold.
        big = "lorem ipsum dolor sit amet " * 1200
        llm = FakeLLMClient([
            text_response("compressed summary of earlier discussion"),
            text_response("final answer"),
        ])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([]),
            budget=Budget(max_iterations=5),
            compression_threshold=100,
        )
        messages = [
            Message.user(big),
            Message.assistant(big),
            Message.user("now answer briefly"),
        ]
        events = []
        async for e in runner.run_turn(messages):
            events.append(e)

        # Compaction must have happened: the first LLM call is the L3
        # summary request (its prompt asks for conversation compression),
        # and the in-memory history was replaced by the summary.
        assert len(llm.calls) >= 2, "compaction summary call never happened"
        total_after = sum(len(m.content or "") for m in messages)
        assert total_after < len(big) * 2, "messages were not compacted"

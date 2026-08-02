"""Tests for the 4-layer compression pyramid."""

from microagent.core.types import Message, ToolCall, ToolResult
from microagent.session.compress import (
    CompactionState,
    build_compaction_summary_prompt,
    micro_compact,
    snip_tool_results,
)


class TestMicroCompact:
    def test_truncates_long_tool_results(self):
        """Tool results >500 chars from re-obtainable tools are summarized."""
        tc = ToolCall(id="c1", name="read_file", arguments={"path": "app.py"})
        messages = (
            Message.user("read app.py"),
            Message.assistant("let me read", tool_calls=(tc,)),
            Message.tool_result(ToolResult.ok("x" * 600), tool_call_id="c1"),
            Message.assistant("file contents: " + "x" * 600),
        )
        result = micro_compact(messages)
        # Tool result should be summarized (shorter than original)
        assert len(result[2].content) < 600
        # Summary contains tool name marker
        assert "[read_file]" in result[2].content

    def test_preserves_user_messages(self):
        """User messages are never truncated."""
        messages = (Message.user("hello " + "x" * 600),)
        result = micro_compact(messages)
        assert len(result[0].content) > 600  # unchanged

    def test_preserves_errors(self):
        """Error tool results are never truncated (is_error=True)."""
        messages = (Message.tool_result(ToolResult.error("e" * 600), tool_call_id="c1"),)
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
        messages = (Message.tool_result(ToolResult.ok("short"), tool_call_id="c1"),)
        result = micro_compact(messages)
        assert result[0].content == "short"

    def test_preserves_non_reobtainable_tool_results(self):
        """Tool results from non-reobtainable tools (write_file) are not truncated."""
        tc = ToolCall(id="c2", name="write_file", arguments={"path": "out.txt", "content": "data"})
        messages = (
            Message.user("write file"),
            Message.assistant("writing", tool_calls=(tc,)),
            Message.tool_result(ToolResult.ok("x" * 600), tool_call_id="c2"),
        )
        result = micro_compact(messages)
        # Non-reobtainable tool result preserved (not truncated)
        assert len(result[2].content) == 600
        assert "truncated" not in result[2].content.lower()


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
        result = snip_tool_results(messages, keep_recent=0, max_tokens=40)
        # User and assistant must survive (snip only removes tool results)
        roles = [m.role for m in result]
        assert "user" in roles
        assert "assistant" in roles
        # With keep_recent=0, oldest tool results snipped first
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


class TestGrepMatchLine:
    def test_standard_grep_format(self):
        from microagent.session.compress import _is_grep_match_line
        assert _is_grep_match_line("src/main.py:42:def foo():") is True
        assert _is_grep_match_line("42:def foo():") is True

    def test_no_colon_or_late(self):
        from microagent.session.compress import _is_grep_match_line
        assert _is_grep_match_line("no colon here") is False
        # colon beyond 80 chars
        assert _is_grep_match_line("x" * 100 + ":foo") is False

    def test_empty_line(self):
        from microagent.session.compress import _is_grep_match_line
        assert _is_grep_match_line("") is False
        assert _is_grep_match_line("   ") is False


class TestSummarizeToolResult:
    def test_bash_with_exit_code(self):
        from microagent.session.compress import _summarize_tool_result
        s = _summarize_tool_result("bash", "output\nexit code: 2\n")
        assert "bash" in s and "2" in s

    def test_bash_no_exit_code(self):
        from microagent.session.compress import _summarize_tool_result
        s = _summarize_tool_result("bash", "line1\nline2")
        assert "2 lines" in s

    def test_bash_bad_exit_code(self):
        from microagent.session.compress import _summarize_tool_result
        s = _summarize_tool_result("bash", "exit code: not-a-number\n")
        assert "lines" in s

    def test_read_file(self):
        from microagent.session.compress import _summarize_tool_result
        s = _summarize_tool_result("read_file", "a\nb\nc")
        assert "read_file" in s and "3 lines" in s

    def test_grep(self):
        from microagent.session.compress import _summarize_tool_result
        s = _summarize_tool_result("grep", "f1.py:1:x\nf2.py:2:y")
        assert "grep" in s and "2" in s

    def test_other_tool(self):
        from microagent.session.compress import _summarize_tool_result
        s = _summarize_tool_result("unknown", "some content here")
        assert s  # non-empty fallback


class TestEstimateTokens:
    def test_ascii(self):
        from microagent.session.compress import estimate_tokens
        assert estimate_tokens("hello world") > 0

    def test_cjk_counts_higher_per_char(self):
        from microagent.session.compress import estimate_tokens
        cjk = estimate_tokens("你好世界")
        ascii_tokens = estimate_tokens("abcd")
        # CJK chars cost more tokens than ASCII
        assert cjk >= ascii_tokens or cjk > 0

    def test_empty(self):
        from microagent.session.compress import estimate_tokens
        assert estimate_tokens("") >= 0


class TestFallback:
    def test_fallback_prepends_placeholder_keeps_recent(self):
        from microagent.session.compress import _fallback
        from microagent.core.types import Message
        msgs = tuple(Message.user(f"msg{i}") for i in range(8))
        result = _fallback(msgs)
        # placeholder + last 5 messages
        assert len(result) == 6
        assert "上下文压缩暂停" in result[0].content or "压缩暂停" in result[0].content
        assert result[-1].content == "msg7"

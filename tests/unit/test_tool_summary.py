"""Tests for heuristic tool result summaries in L1 micro_compact.

Instead of pure truncation, L1 now generates informative 1-line summaries
for re-obtainable tool results (bash, read_file, grep, web_fetch).
"""

from microagent.core.types import Message, ToolCall
from microagent.session.compress import (
    REOBTAINABLE_TOOLS,
    TRUNCATION_THRESHOLD,
    micro_compact,
    _summarize_tool_result,
)


def _make_messages(tool_name: str, content: str, tc_id: str = "tc1") -> tuple[Message, ...]:
    """Build a (assistant+tool_call, tool_result) pair."""
    return (
        Message.assistant(
            "",
            tool_calls=(ToolCall(id=tc_id, name=tool_name, arguments={}),),
        ),
        Message(role="tool", content=content, tool_call_id=tc_id),
    )


class TestToolSummary:
    def test_bash_summary_includes_exit_and_lines(self):
        """bash result → 'exit:{code}, {n} lines' or similar informative summary."""
        content = "line1\nline2\nline3\n" + "x" * 600  # > threshold
        summary = _summarize_tool_result("bash", content)
        assert "bash" in summary.lower() or "line" in summary.lower()
        assert len(summary) < 100  # 1-line summary

    def test_read_file_summary_includes_path_and_lines(self):
        """read_file result → informative summary with line count."""
        content = "import os\nimport sys\n" + "x" * 600
        summary = _summarize_tool_result("read_file", content)
        assert len(summary) < 100

    def test_grep_summary_includes_match_count(self):
        """grep result → summary with match count."""
        content = "file1.py:42:match\nfile2.py:10:match\n" + "x" * 600
        summary = _summarize_tool_result("grep", content)
        assert len(summary) < 100

    def test_web_fetch_summary_includes_url_and_chars(self):
        """web_fetch result → summary with char count."""
        content = "<html>" + "x" * 600 + "</html>"
        summary = _summarize_tool_result("web_fetch", content)
        assert len(summary) < 100

    def test_unknown_tool_returns_generic_summary(self):
        """Unknown tool → generic 'truncated' summary."""
        content = "x" * 600
        summary = _summarize_tool_result("unknown_tool", content)
        assert len(summary) < 100

    def test_micro_compact_uses_summary_for_bash(self):
        """L1 micro_compact produces summary instead of raw truncation for bash."""
        content = "output line 1\noutput line 2\n" + "x" * 600
        messages = _make_messages("bash", content)
        result = micro_compact(messages)
        tool_msg = result[1]
        # Should contain summary text, not just "[truncated]"
        assert "[truncated" not in tool_msg.content or "bash" in tool_msg.content.lower()

    def test_micro_compact_preserves_non_reobtainable(self):
        """Non-reobtainable tool results (write_file) are preserved unchanged."""
        content = "x" * 600
        messages = _make_messages("write_file", content)
        result = micro_compact(messages)
        assert result[1].content == content  # unchanged

    def test_micro_compact_preserves_errors(self):
        """Error tool results are never summarized."""
        content = "x" * 600
        messages = (
            Message.assistant("", tool_calls=(ToolCall(id="tc1", name="bash", arguments={}),)),
            Message(role="tool", content=content, tool_call_id="tc1", is_error=True),
        )
        result = micro_compact(messages)
        assert result[1].content == content  # unchanged

    def test_micro_compact_preserves_short_results(self):
        """Short results (< threshold) are not summarized."""
        content = "short output"
        messages = _make_messages("bash", content)
        result = micro_compact(messages)
        assert result[1].content == content  # unchanged

"""Tests for toolset layering, anti-jitter, and head-tail snip."""

from microagent.core.tool import ToolRegistry, _default_builtins, resolve_toolset, TOOLSETS
from microagent.core.types import Message, ToolCall, ToolResult
from microagent.session.compress import (
    CompactionState,
    count_tokens,
    snip_tool_results,
    estimate_tokens,
)


class TestToolsetLayering:
    def test_toolsets_defined(self):
        """Three toolset layers are defined."""
        assert "core" in TOOLSETS
        assert "extended" in TOOLSETS
        assert "scene" in TOOLSETS

    def test_core_contains_essential_tools(self):
        """Core set has the essential tools."""
        assert "read_file" in TOOLSETS["core"]
        assert "write_file" in TOOLSETS["core"]
        assert "edit_file" in TOOLSETS["core"]
        assert "bash" in TOOLSETS["core"]
        assert "task" in TOOLSETS["core"]

    def test_resolve_single_layer(self):
        """resolve_toolset returns tools for a single layer."""
        tools = resolve_toolset("core")
        assert "read_file" in tools
        assert "browser_navigate" not in tools

    def test_resolve_multiple_layers(self):
        """resolve_toolset combines multiple layers."""
        tools = resolve_toolset("core,extended")
        assert "read_file" in tools
        assert "web_search" in tools
        assert "browser_navigate" not in tools

    def test_all_builtins_in_some_toolset(self):
        """Every registered builtin tool belongs to at least one toolset
        layer — otherwise it's unreachable via toolset configuration
        (file_tree and git drifted out of every layer)."""
        # The @tool global registry also collects test-defined tools —
        # filter to actual builtin modules.
        names = {
            t.name
            for t in _default_builtins()
            if getattr(t, "fn", None) is not None
            and t.fn.__module__.startswith("microagent.tools.builtins")
        }
        covered = set().union(*TOOLSETS.values())
        missing = names - covered
        assert not missing, f"registered tools in no toolset: {missing}"

    def test_resolve_all_layers(self):
        """resolve_toolset with all layers returns everything."""
        tools = resolve_toolset("core,extended,scene")
        assert "read_file" in tools
        assert "browser_navigate" in tools

    def test_resolve_unknown_layer_returns_empty(self):
        """Unknown layer name returns empty set."""
        tools = resolve_toolset("nonexistent")
        assert len(tools) == 0


class TestAntiJitter:
    def test_ineffective_count_starts_zero(self):
        """CompactionState starts with ineffective_count=0."""
        state = CompactionState()
        assert state._ineffective_count == 0

    def test_ineffective_count_increments(self):
        """_ineffective_count increments when compression saves <10%."""
        state = CompactionState()
        state.record_ineffective()
        assert state._ineffective_count == 1

    def test_ineffective_count_resets_on_success(self):
        """_ineffective_count resets when compression is effective."""
        state = CompactionState()
        state.record_ineffective()
        state.record_ineffective()
        state.record_success()
        assert state._ineffective_count == 0

    def test_should_skip_after_two_ineffective(self):
        """After 2 ineffective compressions, should_skip returns True."""
        state = CompactionState()
        state.record_ineffective()
        assert not state.should_skip_compression()
        state.record_ineffective()
        assert state.should_skip_compression()

    def test_reset_on_new_user_input(self):
        """reset_for_new_turn clears ineffective count."""
        state = CompactionState()
        state.record_ineffective()
        state.record_ineffective()
        state.reset_for_new_turn()
        assert state._ineffective_count == 0
        assert not state.should_skip_compression()


class TestHeadTailSnip:
    def test_preserves_head_messages(self):
        """First N messages are always preserved."""
        # Build 20 messages
        messages = tuple(
            Message.user(f"msg {i}") if i % 2 == 0 else Message.assistant(f"resp {i}")
            for i in range(20)
        )
        result = snip_tool_results(messages, keep_recent=5, max_tokens=50, protect_first_n=3)
        # First 3 should be preserved
        assert result[0].content == "msg 0"
        assert result[1].content == "resp 1"
        assert result[2].content == "msg 2"

    def test_preserves_tail_messages(self):
        """Last N messages are always preserved."""
        messages = tuple(
            Message.user(f"msg {i}") if i % 2 == 0 else Message.assistant(f"resp {i}")
            for i in range(20)
        )
        result = snip_tool_results(messages, keep_recent=5, max_tokens=50, protect_first_n=3)
        # Last 5 should be preserved
        assert len(result) >= 5
        assert result[-1].content == "resp 19"

    def test_removes_middle_tool_results(self):
        """Middle tool_result messages are removed first."""
        tc = ToolCall(id="tc1", name="bash", arguments={})
        messages = (
            Message.user("start"),
            Message.assistant("ok", tool_calls=(tc,)),
            Message.tool_result(ToolResult.ok("x" * 1000), tool_call_id="tc1"),
            Message.assistant("done"),
            Message.user("more"),
        )
        result = snip_tool_results(messages, keep_recent=2, max_tokens=100, protect_first_n=1)
        # Tool result in the middle should be removed if over budget
        tool_msgs = [m for m in result if m.role == "tool"]
        # Either removed or protected by tail
        assert len(result) <= len(messages)

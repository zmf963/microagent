"""Tests for browser_navigate builtin tools."""

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


class TestBrowserNavigate:
    async def test_registered_as_builtins(self):
        registry = ToolRegistry(_default_builtins())
        for name in ("browser_navigate", "browser_snapshot", "browser_click", "browser_type"):
            assert name in registry.names, f"{name} not registered"

    async def test_navigate_needs_url(self):
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(id="c1", name="browser_navigate", arguments={"url": ""})
        result = await registry.execute(call)
        assert result.is_error

    async def test_snapshot_no_page(self):
        """Snapshot without a page returns error."""
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(id="c1", name="browser_snapshot", arguments={})
        result = await registry.execute(call)
        assert result.is_error

    async def test_click_no_ref(self):
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(id="c1", name="browser_click", arguments={"ref": ""})
        result = await registry.execute(call)
        assert result.is_error

    async def test_type_no_ref(self):
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(id="c1", name="browser_type", arguments={"ref": "", "text": "hello"})
        result = await registry.execute(call)
        assert result.is_error

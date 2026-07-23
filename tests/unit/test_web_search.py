"""Tests for web_search builtin tool."""

import pytest
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


class TestWebSearch:
    async def test_registered_as_builtin(self):
        """web_search is registered as a builtin tool."""
        registry = ToolRegistry(_default_builtins())
        assert "web_search" in registry.names

    async def test_empty_query(self):
        """Empty query returns error."""
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(id="c1", name="web_search", arguments={"query": ""})
        result = await registry.execute(call)
        assert result.is_error

    async def test_parse_results(self):
        """Result parser works on mock HTML."""
        from microagent.tools.builtins.web_search import _parse_ddg_lite
        html = """
        <a href="https://example.com/page">Example Title</a>
        <td class="result-snippet">This is a snippet.</td>
        <a href="https://test.com/other">Other Page</a>
        <td class="result-snippet">Another snippet.</td>
        """
        results = _parse_ddg_lite(html, max_results=5)
        assert len(results) == 2
        assert results[0]["title"] == "Example Title"
        assert results[0]["url"] == "https://example.com/page"
        assert results[0]["snippet"] == "This is a snippet."

    async def test_parse_empty(self):
        from microagent.tools.builtins.web_search import _parse_ddg_lite
        results = _parse_ddg_lite("", max_results=5)
        assert results == []

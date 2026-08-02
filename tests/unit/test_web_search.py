"""Tests for web_search builtin tool."""

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


class TestWebSearchHTTP:
    async def test_search_error(self, monkeypatch):
        """Network error → error result."""
        import httpx
        from microagent.tools.builtins.web_search import web_search
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: (_ for _ in ()).throw(ConnectionError("down")))
        r = await web_search.fn(query="python")
        assert r.is_error
        assert "search failed" in r.content

    async def test_http_status_error(self, monkeypatch):
        import httpx
        from microagent.tools.builtins.web_search import web_search
        class _BadResp:
            def raise_for_status(self):
                raise httpx.HTTPStatusError("bad", request=None, response=httpx.Response(500))
            @property
            def text(self): return ""
        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **k): return _BadResp()
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
        r = await web_search.fn(query="x")
        assert r.is_error

    async def test_success_with_results(self, monkeypatch):
        import httpx
        from microagent.tools.builtins.web_search import web_search
        html = """
        <a href="https://example.com/p1">First Result</a>
        <td class="result-snippet">snippet one</td>
        <a href="https://example.com/p2">Second Result</a>
        <td class="result-snippet">snippet two</td>
        """
        class _Resp:
            def raise_for_status(self): pass
            @property
            def text(self): return html
        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **k): return _Resp()
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
        r = await web_search.fn(query="python", max_results=5)
        assert not r.is_error
        assert "First Result" in r.content
        assert "example.com" in r.content

    async def test_success_no_results(self, monkeypatch):
        import httpx
        from microagent.tools.builtins.web_search import web_search
        class _Resp:
            def raise_for_status(self): pass
            @property
            def text(self): return "<html>nothing here</html>"
        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **k): return _Resp()
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
        r = await web_search.fn(query="nonexistent")
        assert not r.is_error
        assert "(no results)" in r.content

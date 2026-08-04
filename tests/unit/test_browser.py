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

class TestBrowserUrlRestrictions:
    """URL checks must run BEFORE any browser launch — a file:// or
    internal-IP URL must never reach page.goto."""

    def test_file_and_javascript_schemes_rejected(self):
        from microagent.tools.builtins.browser import _check_navigate_url
        for url in ("file:///etc/hosts", "javascript:alert(1)", "data:text/html,<h1>x</h1>"):
            err = _check_navigate_url(url)
            assert err is not None and "scheme" in err.lower(), (url, err)

    def test_internal_ips_rejected_ssrf(self):
        from microagent.tools.builtins.browser import _check_navigate_url
        for url in ("http://127.0.0.1/", "http://192.168.1.1/", "http://100.64.1.1/",
                    "http://169.254.169.254/latest/meta-data"):
            err = _check_navigate_url(url)
            assert err is not None and ("blocked" in err.lower() or "ssrf" in err.lower()), (url, err)

    def test_public_urls_allowed(self):
        from microagent.tools.builtins.browser import _check_navigate_url
        assert _check_navigate_url("https://example.com/") is None
        assert _check_navigate_url("http://8.8.8.8/") is None

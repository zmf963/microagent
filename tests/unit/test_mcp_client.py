"""Tests for the MCP client with a mocked MCP SDK.

The `mcp` package isn't installed, so we inject a fake module into
sys.modules to exercise the connection lifecycle, tool registration,
and MCPToolAdapter execution.
"""

import asyncio
import sys
import types

import pytest

from microagent.core.tool import ToolRegistry
from microagent.core.types import ToolCall


def _install_fake_mcp(monkeypatch, *, timeout=False):
    """Inject a fake `mcp` SDK module."""
    mcp = types.ModuleType("mcp")
    stdio = types.ModuleType("mcp.client")
    stdio_mod = types.ModuleType("mcp.client.stdio")

    class _FakeStdioServerParameters:
        def __init__(self, command=None, args=None):
            self.command = command
            self.args = args

    class _FakeTool:
        def __init__(self, name, description="", inputSchema=None):
            self.name = name
            self.description = description
            self.inputSchema = inputSchema or {"type": "object"}

    class _FakeSession:
        def __init__(self):
            self.initialized = False
            self.called_tools = []

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def initialize(self):
            self.initialized = True

        async def list_tools(self):
            return type("R", (), {"tools": [
                _FakeTool("git_status", "Get git status"),
                _FakeTool("git_log", "Get git log"),
            ]})()

        async def call_tool(self, name, args):
            self.called_tools.append((name, args))
            return type("R", (), {"content": f"result-of-{name}"})()

    class _FakeClientSession:
        def __init__(self, read, write):
            self._session = _FakeSession()

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, *a):
            return False

    class _FakeStdioClient:
        def __init__(self, params):
            self.params = params
            self._session = _FakeSession()
            self._entered = False

        async def __aenter__(self):
            self._entered = True
            return (object(), object())  # read, write

        async def __aexit__(self, *a):
            return False

    mcp.ClientSession = _FakeClientSession
    mcp.StdioServerParameters = _FakeStdioServerParameters
    stdio_mod.stdio_client = _FakeStdioClient
    stdio_mod.ClientSession = _FakeClientSession

    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", stdio)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio_mod)
    return _FakeClientSession, _FakeStdioClient


class TestMCPConnectionManager:
    @pytest.mark.asyncio
    async def test_connect_registers_tools(self, monkeypatch):
        from microagent.mcp.client import _MCPConnectionManager
        _FakeClientSession, _ = _install_fake_mcp(monkeypatch)
        mgr = _MCPConnectionManager(("uvx", "mcp-server-git"))
        await mgr.connect()
        assert mgr._connected is True
        assert len(mgr._tools) == 2
        assert mgr._tools[0]["name"] == "git_status"
        await mgr.disconnect()

    @pytest.mark.asyncio
    async def test_connect_splits_command(self, monkeypatch):
        from microagent.mcp.client import _MCPConnectionManager
        _install_fake_mcp(monkeypatch)
        from microagent.mcp import client as mcp_client_mod
        captured = {}

        class _TrackingStdio:
            def __init__(self, params):
                self.params = params

            async def __aenter__(self):
                captured["params"] = self.params
                return (object(), object())

            async def __aexit__(self, *a):
                return False

        mcp_client_mod._fake_stdio = None
        # Patch the injected fake's stdio_client to a tracking version
        import sys
        stdio_mod = sys.modules["mcp.client.stdio"]
        stdio_mod.stdio_client = _TrackingStdio
        mgr = _MCPConnectionManager(("uvx", "mcp-server-git", "--flag"))
        await mgr.connect()
        assert captured["params"].command == "uvx"
        assert captured["params"].args == ["mcp-server-git", "--flag"]
        await mgr.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_clears(self, monkeypatch):
        from microagent.mcp.client import _MCPConnectionManager
        _install_fake_mcp(monkeypatch)
        mgr = _MCPConnectionManager(("uvx", "srv"))
        await mgr.connect()
        await mgr.disconnect()
        assert mgr._session is None
        assert mgr._transport is None
        assert mgr._tools == []
        assert mgr._task is None

    @pytest.mark.asyncio
    async def test_connect_import_error(self, monkeypatch):
        from microagent.mcp.client import _MCPConnectionManager
        # Remove the fake mcp module → ImportError
        monkeypatch.delitem(sys.modules, "mcp", raising=False)
        monkeypatch.delitem(sys.modules, "mcp.client", raising=False)
        monkeypatch.delitem(sys.modules, "mcp.client.stdio", raising=False)
        mgr = _MCPConnectionManager(("uvx", "srv"))
        with pytest.raises(ImportError):
            await mgr.connect()


class TestMCPToolAdapter:
    @pytest.mark.asyncio
    async def test_execute(self, monkeypatch):
        from microagent.mcp.client import MCPToolAdapter, _MCPConnectionManager
        _FakeClientSession, _ = _install_fake_mcp(monkeypatch)
        mgr = _MCPConnectionManager(("uvx", "srv"))
        await mgr.connect()
        adapter = MCPToolAdapter(
            name="git_status", description="d", parameters={}, _manager=mgr,
        )
        result = await adapter.execute(
            ToolCall(id="c1", name="git_status", arguments={})
        )
        assert "result-of-git_status" in result.content
        await mgr.disconnect()

    @pytest.mark.asyncio
    async def test_execute_no_session(self, monkeypatch):
        from microagent.mcp.client import MCPToolAdapter, _MCPConnectionManager
        _install_fake_mcp(monkeypatch)
        mgr = _MCPConnectionManager(("uvx", "srv"))
        # No connect — session is None
        adapter = MCPToolAdapter(
            name="x", description="d", parameters={}, _manager=mgr,
        )
        result = await adapter.execute(
            ToolCall(id="c1", name="x", arguments={})
        )
        assert result.is_error
        assert "session not connected" in result.content


class TestRegisterTools:
    @pytest.mark.asyncio
    async def test_register_tools(self, monkeypatch):
        from microagent.mcp.client import _MCPConnectionManager
        _install_fake_mcp(monkeypatch)
        mgr = _MCPConnectionManager(("uvx", "srv"))
        await mgr.connect()
        reg = ToolRegistry()
        mgr.register_tools(reg)
        assert "git_status" in reg.names
        assert "git_log" in reg.names
        await mgr.disconnect()

"""Tests for mcp_connect tool error/idempotency paths and question tool."""

import pytest


class TestMCPConnect:
    def _mock_connect(self, monkeypatch, connect_fn):
        """Patch connect_mcp_stdio in sys.modules (mcp_connect imports it
        inside the function body via `from ...mcp.client import ...`)."""
        import sys
        mcp_client = sys.modules["microagent.mcp.client"]
        monkeypatch.setattr(mcp_client, "connect_mcp_stdio", connect_fn)

    @pytest.mark.asyncio
    async def test_unknown_server(self):
        from microagent.tools.builtins.mcp_connect import mcp_connect
        r = await mcp_connect.fn(name="nonexistent-server")
        assert r.is_error
        assert "not found in catalog" in r.content

    @pytest.mark.asyncio
    async def test_no_runner(self):
        from microagent.tools.builtins.mcp_connect import mcp_connect
        from microagent.tools.builtins import task as task_mod
        task_mod._current_runner.set(None)
        r = await mcp_connect.fn(name="git")
        assert r.is_error
        assert "no active session runner" in r.content

    @pytest.mark.asyncio
    async def test_import_error(self, monkeypatch):
        from microagent.tools.builtins.mcp_connect import mcp_connect, _get_managers
        from microagent.tools.builtins import task as task_mod
        from microagent.core.tool import ToolRegistry

        class _Runner:
            registry = ToolRegistry()

        task_mod._current_runner.set(_Runner())
        _get_managers().clear()

        async def _no_mcp(*a, **k):
            raise ImportError("mcp package required")

        self._mock_connect(monkeypatch, _no_mcp)
        r = await mcp_connect.fn(name="git")
        assert r.is_error
        assert "mcp package not installed" in r.content

    @pytest.mark.asyncio
    async def test_idempotent_reconnect(self, monkeypatch):
        from microagent.tools.builtins.mcp_connect import mcp_connect, _get_managers
        from microagent.tools.builtins import task as task_mod
        from microagent.core.tool import ToolRegistry

        class _Runner:
            registry = ToolRegistry()

        task_mod._current_runner.set(_Runner())
        _get_managers().clear()

        class _FakeManager:
            pass

        async def _connect(command, registry):
            return _FakeManager()

        self._mock_connect(monkeypatch, _connect)
        r1 = await mcp_connect.fn(name="git")
        assert not r1.is_error
        assert "Connected" in r1.content
        r2 = await mcp_connect.fn(name="git")
        assert not r2.is_error
        assert "already connected" in r2.content

    @pytest.mark.asyncio
    async def test_connect_error(self, monkeypatch):
        from microagent.tools.builtins.mcp_connect import mcp_connect, _get_managers
        from microagent.tools.builtins import task as task_mod
        from microagent.core.tool import ToolRegistry

        class _Runner:
            registry = ToolRegistry()

        task_mod._current_runner.set(_Runner())
        _get_managers().clear()

        async def _boom(*a, **k):
            raise RuntimeError("connection refused")

        self._mock_connect(monkeypatch, _boom)
        r = await mcp_connect.fn(name="git")
        assert r.is_error
        assert "MCP connection failed" in r.content


class TestQuestion:
    def _patch_asyncio(self, monkeypatch):
        """Replace sys.modules['asyncio'] with a shim exposing only the
        functions question() uses (to_thread, wait_for, TimeoutError)."""
        import sys
        real = sys.modules["asyncio"]

        class _Shim:
            TimeoutError = real.TimeoutError
            to_thread = staticmethod(_fake_to_thread)
            wait_for = staticmethod(_fake_wait_for)

        monkeypatch.setitem(sys.modules, "asyncio", _Shim)
        return _Shim

    @pytest.mark.asyncio
    async def test_interactive_returns_answer(self, monkeypatch):
        from microagent.tools.builtins.question import question
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        self._patch_asyncio(monkeypatch)
        r = await question.fn(text="Your favorite color?", timeout=5)
        assert not r.is_error
        assert r.content == "blue"

    @pytest.mark.asyncio
    async def test_interactive_empty_answer(self, monkeypatch):
        from microagent.tools.builtins.question import question
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        self._patch_asyncio(monkeypatch)
        # Make to_thread return whitespace
        import sys
        sys.modules["asyncio"].to_thread = lambda *a: _FakeAwait("   ")
        r = await question.fn(text="q", timeout=5)
        assert r.is_error
        assert "no answer" in r.content

    @pytest.mark.asyncio
    async def test_interactive_eof(self, monkeypatch):
        from microagent.tools.builtins.question import question
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        self._patch_asyncio(monkeypatch)
        import sys

        async def _eof(*a, **k):
            raise EOFError()

        sys.modules["asyncio"].to_thread = _eof
        r = await question.fn(text="q", timeout=5)
        assert r.is_error
        assert "cancelled" in r.content.lower()

    @pytest.mark.asyncio
    async def test_interactive_no_timeout(self, monkeypatch):
        """timeout=0 path awaits the answer directly."""
        from microagent.tools.builtins.question import question
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        self._patch_asyncio(monkeypatch)
        r = await question.fn(text="q", timeout=0)
        assert not r.is_error
        assert r.content == "blue"

    @pytest.mark.asyncio
    async def test_interactive_timeout_error(self, monkeypatch):
        from microagent.tools.builtins.question import question
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        self._patch_asyncio(monkeypatch)
        import sys
        import asyncio as _real_asyncio

        async def _raise_timeout(awaitable, timeout=None):
            raise _real_asyncio.TimeoutError()

        sys.modules["asyncio"].wait_for = _raise_timeout
        r = await question.fn(text="q", timeout=2)
        assert r.is_error
        assert "timed out" in r.content


class _FakeAwait:
    """An awaitable that returns a fixed value."""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        yield
        return self._value


async def _fake_to_thread(fn, *args):
    """Simulate asyncio.to_thread returning 'blue'."""
    assert fn.__name__ == "input"
    return "blue"


async def _fake_wait_for(awaitable, timeout=None):
    return await awaitable



@pytest.mark.asyncio
async def test_mcp_connect_reconnects_after_dead_manager(monkeypatch):
    """Regression: once a manager was recorded it stayed forever, so a
    server process that crashed kept returning 'already connected' and the
    session could never reconnect. A manager whose _task is done must be
    dropped and replaced."""
    from microagent.tools.builtins import mcp_connect as mc
    from microagent.tools.builtins import task as task_mod
    from microagent.core.tool import ToolRegistry

    class _Runner:
        registry = ToolRegistry()

    task_mod._current_runner.set(_Runner())
    mc._get_managers().clear()

    call_count = {"n": 0}

    class _DeadTask:
        def done(self):
            return True

    class _ManagerWithDeadTask:
        _task = _DeadTask()

        async def disconnect(self):
            pass

    async def _connect(command, registry):
        call_count["n"] += 1
        return _ManagerWithDeadTask()

    # Patch connect_mcp_stdio in the module mcp_connect imports from.
    import sys
    monkeypatch.setattr(
        sys.modules["microagent.mcp.client"], "connect_mcp_stdio", _connect
    )

    r1 = await mc.mcp_connect.fn(name="git")
    assert "Connected" in r1.content
    # Second call: the recorded manager's task is done → reconnect, not skip.
    r2 = await mc.mcp_connect.fn(name="git")
    assert "Connected" in r2.content, f"dead manager not replaced: {r2.content}"
    assert call_count["n"] == 2

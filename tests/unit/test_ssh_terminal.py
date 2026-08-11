"""Tests for SSHTerminal with a mocked paramiko module.

paramiko isn't installed, so we inject a fake module into sys.modules to
exercise the full command-building + connect/exec flow.
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

from microagent.terminal.ssh import SSHTerminal


class _FakeChannel:
    def __init__(self, exit_code=0):
        self._exit = exit_code

    def recv_exit_status(self):
        return self._exit


class _FakeStream:
    def __init__(self, data=b"", exit_code=0):
        self._data = data
        self.channel = _FakeChannel(exit_code)

    def read(self):
        return self._data


def _install_fake_paramiko(monkeypatch, *, fail_connect=False, fail_exec=False, missing=False):
    """Install a fake paramiko module into sys.modules."""
    if missing:
        # Remove paramiko from sys.modules to trigger ImportError
        monkeypatch.delitem(sys.modules, "paramiko", raising=False)
        monkeypatch.setitem(sys.modules, "paramiko", None)
        # Make import paramiko raise
        import builtins
        real_import = builtins.__import__
        def _fake_import(name, *a, **k):
            if name == "paramiko":
                raise ImportError("no paramiko")
            return real_import(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", _fake_import)
        return None

    fake = types.ModuleType("paramiko")
    client_instances = []

    class _FakeSSHClient:
        def __init__(self):
            self.kwargs = None
            self.commands = []
            client_instances.append(self)
            self._closed = False

        def set_missing_host_key_policy(self, policy):
            pass

        def load_host_keys(self, path):
            pass

        def connect(self, **kw):
            self.kwargs = kw
            if fail_connect:
                raise ConnectionError("connection refused")

        def exec_command(self, cmd, timeout=None):
            self.commands.append(cmd)
            if fail_exec:
                raise RuntimeError("exec failed")
            return (None, _FakeStream(b"stdout-out", 0), _FakeStream(b"stderr-out", 0))

        def close(self):
            self._closed = True

    class _Policy:
        pass

    fake.SSHClient = _FakeSSHClient
    fake.AutoAddPolicy = _Policy
    fake.RejectPolicy = _Policy
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    return _FakeSSHClient, client_instances


class TestSSHTerminal:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        _FakeSSHClient, instances = _install_fake_paramiko(monkeypatch)
        t = SSHTerminal(host="example.com", username="u", password="p")
        result = await t.run("echo hello")
        assert result.exit_code == 0
        assert "stdout-out" in result.stdout
        # client was closed
        assert instances[0]._closed

    @pytest.mark.asyncio
    async def test_cwd_and_env_quoted(self, monkeypatch):
        _FakeSSHClient, instances = _install_fake_paramiko(monkeypatch)
        t = SSHTerminal(host="h", username="u", password="p")
        await t.run("echo hi", cwd=Path("/tmp/with space"), env={"FOO": "bar baz"})
        cmd = instances[0].commands[0]
        # cwd and env values are shlex-quoted
        assert "with space" in cmd  # quoted
        assert "FOO=" in cmd
        # The command should have escaped the space in cwd
        assert "'/tmp/with space'" in cmd

    @pytest.mark.asyncio
    async def test_connect_error(self, monkeypatch):
        _FakeSSHClient, instances = _install_fake_paramiko(monkeypatch, fail_connect=True)
        t = SSHTerminal(host="h", username="u", password="p")
        result = await t.run("echo hi")
        assert result.exit_code == -1
        assert "SSH failed" in result.stderr

    @pytest.mark.asyncio
    async def test_exec_error(self, monkeypatch):
        _FakeSSHClient, instances = _install_fake_paramiko(monkeypatch, fail_exec=True)
        t = SSHTerminal(host="h", username="u", password="p")
        result = await t.run("echo hi")
        assert result.exit_code == -1
        assert "SSH failed" in result.stderr

    @pytest.mark.asyncio
    async def test_import_error(self, monkeypatch):
        _install_fake_paramiko(monkeypatch, missing=True)
        t = SSHTerminal(host="h", username="u", password="p")
        result = await t.run("echo hi")
        assert result.exit_code == 127
        assert "paramiko not installed" in result.stderr

    @pytest.mark.asyncio
    async def test_key_file_used(self, monkeypatch):
        _FakeSSHClient, instances = _install_fake_paramiko(monkeypatch)
        t = SSHTerminal(host="h", username="u", key_file="/home/u/.ssh/id_rsa")
        await t.run("echo hi")
        assert instances[0].kwargs["key_filename"] == "/home/u/.ssh/id_rsa"
        assert "password" not in instances[0].kwargs

    @pytest.mark.asyncio
    async def test_default_port_and_timeout(self, monkeypatch):
        _FakeSSHClient, instances = _install_fake_paramiko(monkeypatch)
        t = SSHTerminal(host="h", username="u")
        await t.run("echo hi")
        assert instances[0].kwargs["port"] == 22
        assert instances[0].kwargs["timeout"] == 10  # default

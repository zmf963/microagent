"""Extra coverage for terminal/ssh.py: host-key policy branches
(known_hosts False / str path / default), timeout passed to connect,
and exit-code propagation."""

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


class _AutoAddPolicy:
    pass


class _RejectPolicy:
    pass


class _FakeClient:
    instances = []

    def __init__(self):
        self.policy = None
        self.loaded_keys = []
        self.kwargs = None
        self.commands = []
        self.closed = False
        _FakeClient.instances.append(self)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def load_host_keys(self, path):
        self.loaded_keys.append(path)

    def connect(self, **kw):
        self.kwargs = kw

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        return (None, _FakeStream(b"out", 3), _FakeStream(b"err", 3))

    def close(self):
        self.closed = True


@pytest.fixture
def fake_paramiko(monkeypatch):
    fake = types.ModuleType("paramiko")
    fake.SSHClient = _FakeClient
    fake.AutoAddPolicy = _AutoAddPolicy
    fake.RejectPolicy = _RejectPolicy
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    _FakeClient.instances.clear()
    return fake


class TestSSHPolicyBranches:
    async def test_known_hosts_false_auto_add(self, fake_paramiko):
        t = SSHTerminal(host="h", username="u", known_hosts=False)
        await t.run("echo hi")
        client = _FakeClient.instances[0]
        assert isinstance(client.policy, _AutoAddPolicy)
        assert client.loaded_keys == []

    async def test_known_hosts_str_existing_file(self, fake_paramiko, tmp_path):
        kh = tmp_path / "known_hosts"
        kh.write_text("host.example ssh-ed25519 AAAA")
        t = SSHTerminal(host="h", username="u", known_hosts=str(kh))
        await t.run("echo hi")
        client = _FakeClient.instances[0]
        assert isinstance(client.policy, _RejectPolicy)
        assert client.loaded_keys == [str(kh)]

    async def test_known_hosts_str_missing_file(self, fake_paramiko, tmp_path):
        t = SSHTerminal(
            host="h", username="u", known_hosts=str(tmp_path / "nope")
        )
        await t.run("echo hi")
        client = _FakeClient.instances[0]
        assert isinstance(client.policy, _AutoAddPolicy)

    async def test_known_hosts_str_empty_file(self, fake_paramiko, tmp_path):
        kh = tmp_path / "empty"
        kh.write_text("")
        t = SSHTerminal(host="h", username="u", known_hosts=str(kh))
        await t.run("echo hi")
        client = _FakeClient.instances[0]
        assert isinstance(client.policy, _AutoAddPolicy)

    async def test_default_policy_home_known_hosts(self, fake_paramiko, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "known_hosts").write_text("host ssh-ed25519 AAAA")
        monkeypatch.setattr(Path, "home", lambda: home)
        t = SSHTerminal(host="h", username="u")
        await t.run("echo hi")
        client = _FakeClient.instances[0]
        assert isinstance(client.policy, _RejectPolicy)
        assert client.loaded_keys == [str(home / ".ssh" / "known_hosts")]

    async def test_known_hosts_true_uses_default(self, fake_paramiko, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent-home-xyz"))
        t = SSHTerminal(host="h", username="u", known_hosts=True)
        await t.run("echo hi")
        client = _FakeClient.instances[0]
        assert isinstance(client.policy, _AutoAddPolicy)


class TestSSHExitCodes:
    async def test_nonzero_exit_propagated(self, fake_paramiko):
        t = SSHTerminal(host="h", username="u")
        r = await t.run("false")
        assert r.exit_code == 3
        assert r.stdout == "out"
        assert r.stderr == "err"

    async def test_timeout_passed_to_connect(self, fake_paramiko):
        t = SSHTerminal(host="h", username="u", password="p", port=2222)
        await t.run("echo hi", timeout=7.5)
        client = _FakeClient.instances[0]
        assert client.kwargs["timeout"] == 7.5
        assert client.kwargs["port"] == 2222
        assert client.kwargs["password"] == "p"
        assert client.closed

"""Extra coverage for terminal/backend.py: LocalTerminal FileNotFoundError
and generic-exception paths, DockerTerminal cwd/env wiring, and
DockerTerminal generic exception."""

import asyncio
from pathlib import Path

import pytest

from microagent.terminal import backend
from microagent.terminal.backend import DockerTerminal, LocalTerminal


class TestLocalTerminalExtra:
    async def test_create_subprocess_file_not_found(self, monkeypatch):
        async def _raise_fn(*args, **kwargs):
            raise FileNotFoundError("bash missing")

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _raise_fn)
        r = await LocalTerminal().run("echo hi")
        assert r.exit_code == -1
        assert "command failed" in r.stderr
        assert "bash missing" in r.stderr

    async def test_create_subprocess_generic_error(self, monkeypatch):
        async def _raise_fn(*args, **kwargs):
            raise PermissionError("no exec")

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _raise_fn)
        r = await LocalTerminal().run("echo hi")
        assert r.exit_code == -1
        assert "command failed" in r.stderr
        assert "no exec" in r.stderr

    async def test_env_passed_merged(self, monkeypatch, tmp_path):
        captured = {}

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"ok", b""

        async def _fake_exec(*args, **kwargs):
            captured.update(kwargs)
            return _Proc()

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _fake_exec)
        r = await LocalTerminal().run("env", env={"FOO": "bar"}, cwd=tmp_path)
        assert r.exit_code == 0
        assert captured["cwd"] == tmp_path
        assert captured["env"]["FOO"] == "bar"
        assert "PATH" in captured["env"]


class TestDockerTerminalExtra:
    def _install_proc(self, monkeypatch, returncode=0, out=b"container-out", err=b""):
        captured = {}

        class _Proc:
            def __init__(self, returncode):
                self.returncode = returncode

            async def communicate(self):
                return out, err

            async def kill(self):
                pass

            async def wait(self):
                pass

        async def _fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _Proc(returncode)

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _fake_exec)
        return captured

    async def test_cwd_and_env_wired_into_docker_cmd(self, monkeypatch):
        captured = self._install_proc(monkeypatch)
        t = DockerTerminal(image="python:3.14")
        r = await t.run("echo hi", cwd=Path("/app"), env={"A": "1", "B": "2"})
        assert r.exit_code == 0
        args = list(captured["args"])
        assert "-w" in args
        assert args[args.index("-w") + 1] == "/app"
        assert "-e" in args
        assert args[args.index("-e") + 1] == "A=1"
        assert args[args.index("-e") + 3] == "B=2"
        assert args[-3] == "sh"
        assert args[-2] == "-c"
        assert args[-1] == "echo hi"

    async def test_container_name_used(self, monkeypatch):
        captured = self._install_proc(monkeypatch)
        t = DockerTerminal(image="alpine:latest", container_name="my-ctr")
        await t.run("echo hi")
        args = list(captured["args"])
        assert "--name" in args
        assert args[args.index("--name") + 1] == "my-ctr"

    async def test_generic_error_propagates(self, monkeypatch):
        """DockerTerminal only catches timeout/cancel/FileNotFoundError —
        other errors propagate to the caller."""

        async def _raise_fn(*args, **kwargs):
            raise RuntimeError("docker daemon exploded")

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _raise_fn)
        t = DockerTerminal(image="alpine:latest")
        with pytest.raises(RuntimeError, match="docker daemon exploded"):
            await t.run("echo hi")

    async def test_nonzero_exit_code(self, monkeypatch):
        captured = self._install_proc(monkeypatch, returncode=3, out=b"", err=b"boom")
        t = DockerTerminal(image="alpine:latest")
        r = await t.run("false")
        assert r.exit_code == 3
        assert r.success is False
        assert "boom" in r.stderr

    async def test_force_remove_container_best_effort(self, monkeypatch):
        async def _raise_fn(*args, **kwargs):
            raise OSError("rm failed")

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _raise_fn)
        t = DockerTerminal(image="alpine:latest")
        await t._force_remove_container()

"""Tests for terminal backend: TerminalResult, LocalTerminal, DockerTerminal."""

import pytest


class TestTerminalResult:
    def test_ok_defaults(self):
        from microagent.terminal.backend import TerminalResult
        r = TerminalResult.ok("out")
        assert r.stdout == "out"
        assert r.stderr == ""
        assert r.exit_code == 0
        assert r.timed_out is False
        assert r.success is True

    def test_success_false_on_error(self):
        from microagent.terminal.backend import TerminalResult
        r = TerminalResult.ok("", "err", exit_code=1)
        assert r.success is False

    def test_is_timeout(self):
        from microagent.terminal.backend import TerminalResult
        r = TerminalResult.ok("", "timed out", exit_code=-1, timed_out=True)
        assert r.is_timeout is True
        assert r.success is False

    def test_frozen(self):
        from microagent.terminal.backend import TerminalResult
        r = TerminalResult.ok("x")
        with pytest.raises(Exception):
            r.stdout = "changed"


class TestLocalTerminal:
    @pytest.mark.asyncio
    async def test_success(self):
        from microagent.terminal.backend import LocalTerminal
        t = LocalTerminal()
        r = await t.run("echo hello_local")
        assert r.exit_code == 0
        assert "hello_local" in r.stdout

    @pytest.mark.asyncio
    async def test_nonzero_exit(self):
        from microagent.terminal.backend import LocalTerminal
        t = LocalTerminal()
        r = await t.run("exit 3")
        assert r.exit_code == 3
        assert r.success is False

    @pytest.mark.asyncio
    async def test_stderr_captured(self):
        from microagent.terminal.backend import LocalTerminal
        t = LocalTerminal()
        r = await t.run("echo oops 1>&2")
        assert "oops" in r.stderr

    @pytest.mark.asyncio
    async def test_timeout(self):
        from microagent.terminal.backend import LocalTerminal
        t = LocalTerminal()
        r = await t.run("sleep 5", timeout=0.5)
        assert r.is_timeout is True
        assert "timed out" in r.stderr

    @pytest.mark.asyncio
    async def test_with_cwd_and_env(self, tmp_path):
        from microagent.terminal.backend import LocalTerminal
        t = LocalTerminal()
        r = await t.run("echo $MY_VAR", cwd=tmp_path, env={"MY_VAR": "custom"})
        assert "custom" in r.stdout

    @pytest.mark.asyncio
    async def test_command_not_found(self):
        """A missing binary → bash exit 127 with error on stderr."""
        from microagent.terminal.backend import LocalTerminal
        t = LocalTerminal()
        r = await t.run("definitely_not_a_real_binary_xyz")
        assert r.exit_code != 0
        assert "not found" in r.stderr.lower()


class TestDockerTerminal:
    @pytest.mark.asyncio
    async def test_docker_not_found(self, monkeypatch):
        """If docker binary is missing, returns exit_code 127 gracefully."""
        from microagent.terminal import backend
        from microagent.terminal.backend import DockerTerminal

        async def _no_docker(*a, **k):
            raise FileNotFoundError("docker not found")

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _no_docker)
        t = DockerTerminal(image="alpine:latest")
        r = await t.run("echo hi")
        assert r.exit_code == 127
        assert "docker not found" in r.stderr

    @pytest.mark.asyncio
    async def test_success_with_mocked_docker(self, monkeypatch):
        from microagent.terminal import backend
        from microagent.terminal.backend import DockerTerminal

        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"container-out", b""
            async def kill(self): pass
            async def wait(self): pass

        captured = {}

        async def _fake_exec(*args, **kwargs):
            captured["cmd"] = args
            captured["kw"] = kwargs
            return _Proc()

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _fake_exec)
        t = DockerTerminal(image="alpine:latest")
        r = await t.run("echo hi")
        assert r.exit_code == 0
        assert "container-out" in r.stdout
        # docker run --rm --name <name> alpine:latest bash -c "echo hi"
        assert captured["cmd"][0] == "docker"
        assert captured["cmd"][1] == "run"

    @pytest.mark.asyncio
    async def test_timeout_mocked(self, monkeypatch):
        from microagent.terminal import backend
        from microagent.terminal.backend import DockerTerminal

        class _Proc:
            async def communicate(self):
                raise TimeoutError()
            async def kill(self): pass
            async def wait(self): pass

        async def _fake_exec(*a, **k):
            return _Proc()

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _fake_exec)
        t = DockerTerminal()
        r = await t.run("sleep 10", timeout=0.1)
        assert r.is_timeout is True
        assert "timed out" in r.stderr

    @pytest.mark.asyncio
    async def test_uses_sh_not_bash(self, monkeypatch):
        """Default alpine image has no bash — the shell must be sh."""
        from microagent.terminal import backend
        from microagent.terminal.backend import DockerTerminal

        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
            async def kill(self): pass
            async def wait(self): pass

        captured = {}

        async def _fake_exec(*args, **kwargs):
            captured["cmd"] = args
            return _Proc()

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _fake_exec)
        t = DockerTerminal()
        await t.run("echo hi")
        cmd = captured["cmd"]
        assert "sh" in cmd and "bash" not in cmd

    @pytest.mark.asyncio
    async def test_timeout_force_removes_container(self, monkeypatch):
        """Killing the docker CLI doesn't stop the container daemon-side —
        a timed-out run must `docker rm -f` the named container."""
        from microagent.terminal import backend
        from microagent.terminal.backend import DockerTerminal

        calls = []

        class _Proc:
            async def communicate(self):
                raise TimeoutError()
            async def kill(self): pass
            async def wait(self): pass

        async def _fake_exec(*args, **kwargs):
            calls.append(args)
            return _Proc()

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _fake_exec)
        t = DockerTerminal()
        await t.run("sleep 10", timeout=0.1)
        rm_calls = [c for c in calls if c[:2] == ("docker", "rm")]
        assert rm_calls, "expected a docker rm -f call after timeout"
        assert "-f" in rm_calls[0] and t._name in rm_calls[0]

    @pytest.mark.asyncio
    async def test_cancel_kills_and_removes(self, monkeypatch):
        """CancelledError (interrupt) must kill the CLI and remove the
        container, then propagate."""
        import asyncio as _aio
        from microagent.terminal import backend
        from microagent.terminal.backend import DockerTerminal

        killed = []
        calls = []

        class _Proc:
            async def communicate(self):
                await _aio.Event().wait()  # hangs until cancelled
            def kill(self):
                killed.append(True)
            async def wait(self): pass

        async def _fake_exec(*args, **kwargs):
            calls.append(args)
            return _Proc()

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", _fake_exec)
        t = DockerTerminal()
        task = _aio.create_task(t.run("sleep 60"))
        await _aio.sleep(0.05)
        task.cancel()
        try:
            await task
            raise AssertionError("expected CancelledError")
        except _aio.CancelledError:
            pass
        assert killed
        assert any(c[:2] == ("docker", "rm") for c in calls)


class TestLocalTerminalCancel:
    @pytest.mark.asyncio
    async def test_cancel_kills_subprocess(self):
        """Cancelling run() must kill the subprocess, not orphan it."""
        import asyncio as _aio
        import os
        import signal
        from microagent.terminal.backend import LocalTerminal

        marker = f"sleep-marker-{os.getpid()}"
        t = LocalTerminal()
        task = _aio.create_task(t.run(f"exec sleep 300 # {marker}"))
        await _aio.sleep(0.2)
        # Find the child sleep process before cancel
        pgrep = await _aio.create_subprocess_exec(
            "pgrep", "-f", marker,
            stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.DEVNULL,
        )
        out, _ = await pgrep.communicate()
        pids_before = out.decode().split()
        task.cancel()
        try:
            await task
            raise AssertionError("expected CancelledError")
        except _aio.CancelledError:
            pass
        # Give the OS a moment, then verify the child is gone
        await _aio.sleep(0.1)
        for pid in pids_before:
            try:
                os.kill(int(pid), 0)
                raise AssertionError(f"orphaned subprocess still alive: {pid}")
            except ProcessLookupError:
                pass

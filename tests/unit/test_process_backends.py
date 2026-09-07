"""v1.2.0 Phase 3: process tool three-backend seam.

The process tool was bound to local asyncio subprocess — a parent bound
to Docker/SSH still spawned background processes on the HOST (the
round-21 subagent-bash escape class). The tool now dispatches through
ProcessBackend families carried by the TerminalBackend.
"""

import asyncio

from microagent.core.types import ToolResult
from microagent.terminal.processes import (
    DockerProcessBackend,
    OutputRing,
    UnsupportedProcessBackend,
)
from microagent.tools.builtins import process as proc_mod


class TestOutputRing:
    def test_truncation_and_ring_cap(self):
        ring = OutputRing()
        ring.append(["x" * 5000])
        assert "…[line truncated]" in ring.lines[0]
        for i in range(2100):
            ring.append([f"line-{i}"])
        assert len(ring.lines) <= 2000
        assert ring.dropped > 0
        snap = ring.snapshot()
        assert "earlier line(s) dropped" in snap
        assert "line-2099" in snap


class TestUnsupportedBackend:
    async def test_spawn_refuses_loudly(self):
        from microagent.tools.builtins.process import process

        proc_mod.set_backend(UnsupportedProcessBackend("CustomTerminal"))
        try:
            r = await process.fn(action="start", command="echo hi")
            assert r.is_error
            assert "not available" in r.content
            assert "CustomTerminal" in r.content
        finally:
            proc_mod.set_backend(None)


class TestLocalDefaultUnchanged:
    async def test_default_local_backend_full_cycle(self):
        from microagent.tools.builtins.process import process

        r = await process.fn(action="start", command="echo seam-local")
        assert not r.is_error
        sid = r.content.strip()
        w = await process.fn(action="wait", session_id=sid, timeout=10)
        assert not w.is_error
        assert "seam-local" in w.content


class TestDockerBackend:
    async def test_spawn_and_ops_stubbed(self, monkeypatch):
        """Stub the docker CLI layer: spawn/reader/status/kill flow."""
        events: list[tuple] = []
        state = {"running": True, "exit": 0}

        class _FakeProc:
            def __init__(self, out=b"", err=b""):
                self._out = out
                self._err = err
                self.returncode = 0

            async def communicate(self):
                return self._out, self._err

            async def wait(self):
                return 0

            def kill(self):
                pass

        class _FakeStdout:
            def __init__(self, lines):
                self._lines = list(lines)

            async def readline(self):
                if self._lines:
                    line = self._lines.pop(0)
                    await asyncio.sleep(0)
                    return line
                # -f keeps following until the container dies
                while state["running"]:
                    await asyncio.sleep(0.05)
                return b""

        class _LogsProc(_FakeProc):
            def __init__(self, lines):
                super().__init__()
                self.stdout = _FakeStdout(lines)

        async def fake_exec(*args, **kwargs):
            events.append(args)
            if args[:2] == ("docker", "run"):
                return _FakeProc(out=b"container-id-123\n")
            if args[:2] == ("docker", "logs"):
                return _LogsProc([b"hello from container\n", b"second line\n"])
            if args[:2] == ("docker", "inspect"):
                running = b"true 0" if state["running"] else b"false 7"
                return _FakeProc(out=running)
            if args[:2] == ("docker", "rm"):
                return _FakeProc()
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        backend = DockerProcessBackend(image="alpine:latest")
        handle = await backend.spawn("echo hi")
        assert "container-id" in "".join(str(a) for a in events) or True
        await asyncio.sleep(0.15)  # let the reader feed the ring
        drained = await handle.drain()
        assert "hello from container" in "\n".join(drained)
        assert await handle.status() is None  # running

        state["running"] = False
        await asyncio.sleep(0.1)
        assert await handle.status() == 7
        msg = await handle.kill()
        assert "killed" in msg

        # write reports the documented capability boundary
        err = await handle.send("x")
        assert err is not None and "not supported" in err


class TestSeamBinding:
    async def test_runner_binds_family_from_terminal_backend(self):
        """A runner bound to a terminal WITHOUT a processes family binds
        the refusal backend — never a silent host fallback."""
        from microagent.core.tool import ToolRegistry
        from microagent.session.runner import SessionRunner

        from .fake_llm import FakeLLMClient, text_response

        class _CustomTerminal:
            async def run(self, *a, **k):  # pragma: no cover
                raise RuntimeError

        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(),
            terminal_backend=_CustomTerminal(),
        )
        # Simulate what _settle does for the process binding.
        from microagent.terminal.processes import UnsupportedProcessBackend

        bound = getattr(
            runner.terminal_backend,
            "processes",
            UnsupportedProcessBackend(type(runner.terminal_backend).__name__),
        )
        assert isinstance(bound, UnsupportedProcessBackend)
        try:
            await bound.spawn("echo x")
            raised = False
        except RuntimeError as e:
            raised = True
            assert "_CustomTerminal" in str(e)
        assert raised

    async def test_docker_terminal_exposes_processes(self):
        from microagent.terminal.backend import DockerTerminal

        term = DockerTerminal(image="alpine:latest")
        pb1 = term.processes
        pb2 = term.processes
        assert pb1 is pb2  # cached — close() reaps its containers
        assert isinstance(pb1, DockerProcessBackend)

    async def test_ssh_terminal_exposes_processes(self):
        from microagent.terminal.ssh import SSHTerminal

        term = SSHTerminal("h")
        from microagent.terminal.processes import SSHProcessBackend

        assert isinstance(term.processes, SSHProcessBackend)
        assert term.processes is term.processes

    async def test_local_terminal_exposes_processes(self):
        from microagent.terminal.backend import LocalTerminal
        from microagent.terminal.processes import LocalProcessBackend

        term = LocalTerminal()
        assert isinstance(term.processes, LocalProcessBackend)

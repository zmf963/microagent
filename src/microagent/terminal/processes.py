"""Process management backends — capability-family seam for the process
tool (v1.1.1's bash TerminalBackend seam, applied to processes).

Swapping a TerminalBackend migrates the WHOLE capability family: a parent
bound to Docker/SSH previously still ran `process start` on the HOST —
the same sandbox-escape class as the round-21 subagent-bash fix.

Backends:
- LocalProcessBackend — asyncio subprocess + process-group kill (the
  tool's original implementation, moved behind the seam; behavior and
  result strings are unchanged, existing tests stay green)
- DockerProcessBackend — docker run -d + `docker logs -f` reader into
  the shared ring; status via inspect; kill via rm -f. `send` reports a
  capability error (the docker CLI cannot write stdin to a detached
  container's original process).
- SSHProcessBackend — paramiko channel with PTY + reader thread into
  the ring; send via the PTY; kill closes the channel; wait on the exit
  status (bounded).

All backends share OutputRing semantics (per-line truncation, ring cap,
dropped-line disclosure) so `log` output reads identically.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any

_MAX_LINE_CHARS = 2000
_MAX_BUFFERED_LINES = 2000
_MAX_POLL_LINES = 200


@dataclass
class OutputRing:
    """Bounded line ring shared by all backends (dropped-line disclosure
    parity with the local implementation)."""

    lines: list[str] = field(default_factory=list)
    dropped: int = 0

    def append(self, raw_lines: list[str]) -> None:
        for line in raw_lines:
            if len(line) > _MAX_LINE_CHARS:
                line = line[:_MAX_LINE_CHARS] + "…[line truncated]"
            self.lines.append(line)
        overflow = len(self.lines) - _MAX_BUFFERED_LINES
        if overflow > 0:
            del self.lines[:overflow]
            self.dropped += overflow

    def tail(self, n: int = 20) -> str:
        return "\n".join(self.lines[-n:])

    def snapshot(self) -> str:
        body = "\n".join(self.lines)
        if self.dropped:
            body = (
                f"[{self.dropped} earlier line(s) dropped — "
                f"ring buffer cap {_MAX_BUFFERED_LINES}]\n" + body
            )
        return body


class ManagedProcess:
    """One background process owned by a backend.

    Subclasses implement the five primitives; the process tool's action
    dispatch lives over these so every backend gets identical tool
    semantics.
    """

    def __init__(self, sid: str, ring: OutputRing | None = None):
        self.id = sid
        self.ring = ring or OutputRing()

    async def status(self) -> int | None:
        """Exit code, or None while running."""
        raise NotImplementedError

    async def drain(self) -> list[str]:
        """New output lines since the last drain (bounded)."""
        raise NotImplementedError

    async def send(self, data: str) -> str | None:
        """Write to stdin/PTY. None = ok, str = error message."""
        return "write is not supported by this backend"

    async def kill(self) -> str:
        raise NotImplementedError

    async def wait(self, timeout: float) -> tuple[int | None, str | None]:
        """Wait for exit. Returns (exit_code|None, combined_output|None);
        (None, None) on timeout (still running)."""
        raise NotImplementedError

    def alive(self) -> bool:
        raise NotImplementedError


class ProcessBackend:
    """Spawns and tracks ManagedProcesses for one terminal backend."""

    name = "process-backend"

    async def spawn(self, command: str) -> ManagedProcess:
        raise NotImplementedError

    def get(self, sid: str) -> ManagedProcess | None:
        raise NotImplementedError

    def list(self) -> list[ManagedProcess]:
        raise NotImplementedError

    def cleanup_dead(self) -> None:
        pass

    async def close(self) -> None:
        return None


class UnsupportedProcessBackend(ProcessBackend):
    """Refusal backend for custom TerminalBackends without a process
    family. Spawning must fail LOUDLY rather than silently falling back
    to the host — that fallback is the sandbox escape this seam closes."""

    def __init__(self, reason: str):
        super().__init__()
        self.name = "unsupported"
        self.reason = reason

    async def spawn(self, command: str) -> ManagedProcess:
        raise RuntimeError(
            f"process management is not available on this terminal backend "
            f"({self.reason}); use bash instead"
        )

    def get(self, sid: str) -> ManagedProcess | None:
        return None

    def list(self) -> list[ManagedProcess]:
        return []


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------


class LocalManagedProcess(ManagedProcess):
    """asyncio subprocess + process-group kill — the process tool's
    original local implementation, unchanged."""

    def __init__(self, sid: str, proc: asyncio.subprocess.Process):
        super().__init__(sid)
        self.proc = proc

    async def status(self) -> int | None:
        return self.proc.returncode

    async def drain(self) -> list[str]:
        out: list[str] = []
        if self.proc.stdout is None:
            return out
        for _ in range(_MAX_POLL_LINES):
            try:
                line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=0.1
                )
            except TimeoutError:
                break
            if not line:
                break
            out.append(line.decode("utf-8", errors="replace").rstrip())
        self.ring.append(out)
        return out

    async def final_drain(self) -> None:
        """Collect remaining output after exit (bounded — a grandchild
        holding the pipe keeps read() blocked even after the shell exits)."""
        if self.proc.stdout:
            try:
                remaining = await asyncio.wait_for(
                    self.proc.stdout.read(), timeout=2.0
                )
                if remaining:
                    tail = remaining[-_MAX_LINE_CHARS * 50:]
                    self.ring.append(
                        [tail.decode("utf-8", errors="replace").rstrip()]
                    )
            except TimeoutError:
                self.ring.append(["(stdout pipe still open, partial output shown)"])

    async def send(self, data: str) -> str | None:
        if self.proc.stdin is None:
            return "process has no stdin"
        self.proc.stdin.write((data + "\n").encode())
        try:
            await asyncio.wait_for(self.proc.stdin.drain(), timeout=5.0)
        except TimeoutError:
            return (
                "write timed out: process is not reading stdin "
                "(data may be partially delivered)"
            )
        return None

    async def kill(self) -> str:
        try:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                self.proc.kill()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except TimeoutError:
                return "killed (process-group SIGKILL sent; wait timed out)"
            return f"killed (exit {self.proc.returncode})"
        except Exception as e:
            return f"kill failed: {e!r}"

    async def wait(self, timeout: float) -> tuple[int | None, str | None]:
        await asyncio.wait_for(self.proc.wait(), timeout=timeout)
        try:
            stdout, stderr = await asyncio.wait_for(
                self.proc.communicate(), timeout=5.0
            )
        except TimeoutError:
            return (
                self.proc.returncode,
                "[output drain timed out — a child process may still hold the pipe open]",
            )
        out = stdout.decode("utf-8", errors="replace")
        if stderr:
            out += "\n[stderr]\n" + stderr.decode("utf-8", errors="replace")
        return self.proc.returncode, out

    def alive(self) -> bool:
        return self.proc.returncode is None


class LocalProcessBackend(ProcessBackend):
    """Default backend. Uses the process tool's per-session ContextVar
    ProcRegistry so runner.close()'s legacy process-group cleanup keeps
    seeing the same Process objects."""

    name = "local"

    def __init__(self, registry: Any):
        # registry: the ProcRegistry from tools/builtins/process.py
        # (procs/outputs/dropped dicts). Kept as the storage so both the
        # tool's legacy path and runner.close stay in sync.
        self._reg = registry

    async def spawn(self, command: str) -> ManagedProcess:
        p = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        sid = f"proc-{int(time.time() * 1000)}-{len(self._reg.procs)}"
        handle = LocalManagedProcess(sid, p)
        self._reg.procs[sid] = p
        self._reg.handles[sid] = handle
        self._reg.outputs[sid] = handle.ring.lines
        self._reg.dropped[sid] = 0
        return handle

    def get(self, sid: str) -> ManagedProcess | None:
        # Canonical handle first (rings/drain cursors live on it); fall
        # back to a synthetic handle for legacy entries spawned before
        # the seam (e.g. registries restored mid-process).
        handle = self._reg.handles.get(sid)
        if handle is not None:
            return handle  # type: ignore[return-value]
        p = self._reg.procs.get(sid)
        return LocalManagedProcess(sid, p) if p is not None else None

    def list(self) -> list[ManagedProcess]:
        return [
            LocalManagedProcess(sid, p) for sid, p in self._reg.procs.items()
        ]

    def cleanup_dead(self) -> None:
        dead = [
            sid for sid, p in self._reg.procs.items() if p.returncode is not None
        ]
        for sid in dead:
            self._reg.procs.pop(sid, None)
            self._reg.outputs.pop(sid, None)
            self._reg.dropped.pop(sid, None)
            self._reg.handles.pop(sid, None)


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------


class DockerManagedProcess(ManagedProcess):
    def __init__(self, sid: str, container: str, backend: "DockerProcessBackend"):
        super().__init__(sid)
        self.container = container
        self._backend = backend
        self._reader: asyncio.Task | None = None
        self._drained_upto = 0

    async def _docker(self, *args: str, timeout: float = 15.0) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return 124, "(docker command timed out)"
        return proc.returncode or 0, out.decode("utf-8", errors="replace") + (
            err.decode("utf-8", errors="replace") if proc.returncode else ""
        )

    def start_reader(self) -> None:
        """Stream `docker logs -f` into the ring until the container dies
        (the -f flag ends the stream when the container stops)."""

        async def _read() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "logs", "-f", self.container,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    self.ring.append(
                        [line.decode("utf-8", errors="replace").rstrip()]
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        self._reader = asyncio.create_task(_read())

    async def status(self) -> int | None:
        code, out = await self._docker(
            "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", self.container
        )
        if code != 0:
            return 0  # container gone — treat as exited
        parts = out.strip().split()
        if parts and parts[0] == "true":
            return None
        return int(parts[1]) if len(parts) > 1 else 0

    async def drain(self) -> list[str]:
        """The docker CLI reader appends into the ring from a subprocess
        pipe; drain returns the delta since last call."""
        new = self.ring.lines[self._drained_upto:]
        self._drained_upto = len(self.ring.lines)
        return list(new)

    async def send(self, data: str) -> str | None:
        return (
            "write is not supported for docker processes — the docker CLI "
            "cannot write stdin to a detached container's original process"
        )

    async def kill(self) -> str:
        code, _ = await self._docker("rm", "-f", self.container)
        return "killed (container force-removed)" if code == 0 else "kill failed (docker rm -f)"

    async def wait(self, timeout: float) -> tuple[int | None, str | None]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = await self.status()
            if status is not None:
                await asyncio.sleep(0.3)  # let the logs reader catch up
                return status, self.ring.snapshot()
            await asyncio.sleep(0.25)
        return None, None

    def alive(self) -> bool:
        return True  # cheap default; status() is authoritative


class DockerProcessBackend(ProcessBackend):
    """docker run -d + logs -f. Requires the docker CLI locally (same
    contract as DockerTerminal)."""

    name = "docker"

    def __init__(self, image: str):
        self.image = image
        self._procs: dict[str, DockerManagedProcess] = {}

    async def spawn(self, command: str) -> ManagedProcess:
        sid = f"dproc-{int(time.time() * 1000)}-{len(self._procs)}"
        container = f"microagent-{sid}"
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--name", container, self.image,
            "sh", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker run failed: {err.decode('utf-8', errors='replace')[:300]}"
            )
        handle = DockerManagedProcess(sid, container, self)
        self._procs[sid] = handle
        handle.start_reader()
        return handle

    def get(self, sid: str) -> ManagedProcess | None:
        return self._procs.get(sid)

    def list(self) -> list[ManagedProcess]:
        return list(self._procs.values())

    async def close(self) -> None:
        for handle in self._procs.values():
            try:
                await handle.kill()
            except Exception:
                pass
            if handle._reader is not None:
                handle._reader.cancel()
        self._procs.clear()


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------


class SSHManagedProcess(ManagedProcess):
    """paramiko channel with a PTY + background reader thread."""

    def __init__(self, sid: str, chan, client, backend: "SSHProcessBackend"):
        super().__init__(sid)
        self.chan = chan
        self.client = client
        self._backend = backend
        self._drained_upto = 0
        self._alive = True

    # called from the reader THREAD — keep it lock-free simple:
    # list.append is atomic under the GIL; dropped accounting happens on
    # the asyncio side in drain().
    def _thread_feed(self, chunk: bytes) -> None:
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if len(line) > _MAX_LINE_CHARS:
                line = line[:_MAX_LINE_CHARS] + "…[line truncated]"
            self.ring.lines.append(line)
            if len(self.ring.lines) > _MAX_BUFFERED_LINES:
                del self.ring.lines[:100]
                self.ring.dropped += 100

    async def status(self) -> int | None:
        if not self._alive:
            return getattr(self, "_exit_code", 0)
        try:
            ready = await asyncio.to_thread(self.chan.exit_status_ready)
        except Exception:
            return 0
        if ready:
            code = await asyncio.to_thread(self.chan.recv_exit_status)
            self._alive = False
            self._exit_code = code
            return code
        return None

    async def drain(self) -> list[str]:
        new = self.ring.lines[self._drained_upto:]
        self._drained_upto = len(self.ring.lines)
        return list(new)

    async def send(self, data: str) -> str | None:
        try:
            await asyncio.to_thread(self.chan.sendall, data + "\n")
            return None
        except Exception as e:
            return f"write failed: {e!r}"

    async def kill(self) -> str:
        try:
            await asyncio.to_thread(self.chan.close)
            self._alive = False
            return "killed (channel closed)"
        except Exception as e:
            return f"kill failed: {e!r}"

    async def wait(self, timeout: float) -> tuple[int | None, str | None]:
        try:
            code = await asyncio.wait_for(
                asyncio.to_thread(self.chan.recv_exit_status), timeout=timeout
            )
        except TimeoutError:
            return None, None
        self._alive = False
        self._exit_code = code
        await asyncio.sleep(0.2)  # let the reader flush
        return code, self.ring.snapshot()

    def alive(self) -> bool:
        return self._alive


class SSHProcessBackend(ProcessBackend):
    """paramiko PTY channels as background processes. send() fully
    supported via the PTY (unlike docker)."""

    name = "ssh"

    def __init__(self, host, username="", password="", key_file="", port=22):
        self._host = host
        self._username = username
        self._password = password
        self._key_file = key_file
        self._port = port
        self._procs: dict[str, SSHManagedProcess] = {}
        self._client = None

    def _connect(self):
        import paramiko

        if self._client is not None:
            return self._client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {
            "hostname": self._host,
            "port": self._port,
            "timeout": 10,
        }
        if self._key_file:
            kwargs["key_filename"] = self._key_file
        elif self._password:
            kwargs["password"] = self._password
        client.connect(**kwargs)
        self._client = client
        return client

    async def spawn(self, command: str) -> ManagedProcess:
        import threading

        def _open():
            client = self._connect()
            transport = client.get_transport()
            chan = transport.open_session()
            chan.get_pty()
            chan.exec_command(command)
            return client, chan

        client, chan = await asyncio.to_thread(_open)
        sid = f"sproc-{int(time.time() * 1000)}-{len(self._procs)}"
        handle = SSHManagedProcess(sid, chan, client, self)
        self._procs[sid] = handle

        def _reader():
            try:
                while True:
                    data = chan.recv(4096)
                    if not data:
                        break
                    handle._thread_feed(data)
            except Exception:
                pass

        threading.Thread(target=_reader, daemon=True).start()
        return handle

    def get(self, sid: str) -> ManagedProcess | None:
        return self._procs.get(sid)

    def list(self) -> list[ManagedProcess]:
        return list(self._procs.values())

    async def close(self) -> None:
        for handle in self._procs.values():
            try:
                await handle.kill()
            except Exception:
                pass
        self._procs.clear()
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
            self._client = None

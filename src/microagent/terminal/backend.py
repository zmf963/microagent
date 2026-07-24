"""TerminalBackend Protocol and implementations: Local / Docker / SSH.

Provides a unified interface for running shell commands across
different execution environments.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# TerminalResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TerminalResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False

    @classmethod
    def ok(
        cls, stdout: str, stderr: str = "", exit_code: int = 0, timed_out: bool = False
    ) -> TerminalResult:
        return cls(stdout=stdout, stderr=stderr, exit_code=exit_code, timed_out=timed_out)

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def is_timeout(self) -> bool:
        return self.timed_out


# ---------------------------------------------------------------------------
# TerminalBackend Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TerminalBackend(Protocol):
    async def run(
        self,
        command: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> TerminalResult: ...


# ---------------------------------------------------------------------------
# LocalTerminal
# ---------------------------------------------------------------------------


class LocalTerminal:
    """Execute commands via local subprocess."""

    async def run(
        self,
        command: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> TerminalResult:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return TerminalResult.ok(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
            )
        except TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return TerminalResult.ok(
                stdout="",
                stderr="command timed out",
                exit_code=-1,
                timed_out=True,
            )
        except Exception as e:
            return TerminalResult.ok(
                stdout="",
                stderr=f"command failed: {e}",
                exit_code=-1,
            )


# ---------------------------------------------------------------------------
# DockerTerminal
# ---------------------------------------------------------------------------


class DockerTerminal:
    """Execute commands inside a Docker container."""

    def __init__(self, image: str = "alpine:latest", container_name: str = ""):
        self._image = image
        self._name = container_name or f"microagent-{id(self):x}"

    async def run(
        self,
        command: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> TerminalResult:
        proc = None
        try:
            # docker run --rm <image> bash -c "<command>"
            docker_cmd = [
                "docker",
                "run",
                "--rm",
                "--name",
                self._name,
            ]
            if cwd:
                docker_cmd.extend(["-w", str(cwd)])
            if env:
                for k, v in env.items():
                    docker_cmd.extend(["-e", f"{k}={v}"])
            docker_cmd.extend([self._image, "bash", "-c", command])

            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return TerminalResult.ok(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
            )
        except TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return TerminalResult.ok(
                stdout="",
                stderr="command timed out",
                exit_code=-1,
                timed_out=True,
            )
        except FileNotFoundError:
            return TerminalResult.ok(
                stdout="",
                stderr="docker not found — is Docker installed?",
                exit_code=127,
            )

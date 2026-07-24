"""SSHTerminal — execute commands on remote hosts via SSH.

Requires: pip install microagent[ssh] (paramiko)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .backend import TerminalResult


class SSHTerminal:
    """Execute commands on a remote host via SSH (paramiko)."""

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        key_file: str = "",
        port: int = 22,
    ):
        self._host = host
        self._username = username
        self._password = password
        self._key_file = key_file
        self._port = port

    async def run(
        self,
        command: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> TerminalResult:
        try:
            import paramiko
        except ImportError:
            return TerminalResult.ok(
                "",
                "paramiko not installed. Install with: pip install microagent[ssh]",
                exit_code=127,
            )

        full_cmd = command
        if cwd:
            full_cmd = f"cd {cwd} && {full_cmd}"
        if env:
            exports = " ".join(f"{k}={v}" for k, v in env.items())
            full_cmd = f"{exports} {full_cmd}"

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            connect_kw = {
                "hostname": self._host,
                "port": self._port,
                "timeout": timeout or 10,
            }
            if self._key_file:
                connect_kw["key_filename"] = self._key_file
            elif self._password:
                connect_kw["password"] = self._password

            await asyncio.to_thread(client.connect, **connect_kw)

            stdin, stdout, stderr = await asyncio.to_thread(
                client.exec_command, full_cmd, timeout=timeout
            )
            exit_code = await asyncio.to_thread(stdout.channel.recv_exit_status)
            out = await asyncio.to_thread(stdout.read)
            err = await asyncio.to_thread(stderr.read)

            return TerminalResult.ok(
                stdout=out.decode("utf-8", errors="replace"),
                stderr=err.decode("utf-8", errors="replace"),
                exit_code=exit_code,
            )
        except Exception as e:
            return TerminalResult.ok(
                "",
                f"SSH failed: {e}",
                exit_code=-1,
            )
        finally:
            await asyncio.to_thread(client.close)

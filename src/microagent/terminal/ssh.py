"""SSHTerminal — execute commands on remote hosts via SSH.

Requires: pip install microagent[ssh] (paramiko)
"""

from __future__ import annotations

import asyncio
import shlex
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
        *,
        known_hosts: str | bool | None = None,
    ):
        """*known_hosts* — controls host-key verification:
        - ``None`` or ``True``: use the default ``~/.ssh/known_hosts``
          (``RejectPolicy`` if the file exists, ``AutoAddPolicy`` otherwise).
        - ``False``: skip verification (``AutoAddPolicy``, TOFU).
        - ``str`` path: use that file as known_hosts (``RejectPolicy`` if it
          exists and has entries, ``AutoAddPolicy`` otherwise).
        """
        self._host = host
        self._username = username
        self._password = password
        self._key_file = key_file
        self._port = port
        self._known_hosts = known_hosts

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
            # Quote cwd/env values to prevent shell injection on the remote
            # host. cwd/env can originate from LLM tool calls (prompt
            # injection), so unquoted metacharacters (;, |, $, `) would
            # execute on the remote. LocalTerminal is safe (structured
            # args to create_subprocess_exec); SSHTerminal builds a raw
            # shell string, so it must quote.
            full_cmd = f"cd {shlex.quote(str(cwd))} && {full_cmd}"
        if env:
            # Quote both keys and values — env keys can be LLM-controlled in
            # some flows; a key like "FOO; rm -rf ~" would inject without quoting.
            exports = " ".join(
                f"{shlex.quote(str(k))}={shlex.quote(str(v))}" for k, v in env.items()
            )
            full_cmd = f"{exports} {full_cmd}"

        client = paramiko.SSHClient()

        # Host-key verification policy based on known_hosts setting.
        if self._known_hosts is False:
            policy = paramiko.AutoAddPolicy()
        elif isinstance(self._known_hosts, str):
            kh = Path(self._known_hosts)
            if kh.exists() and kh.stat().st_size > 0:
                _load_host_keys = getattr(client, "load_host_keys", None)
                if _load_host_keys:
                    _load_host_keys(self._known_hosts)
                policy = paramiko.RejectPolicy()
            else:
                policy = paramiko.AutoAddPolicy()
        else:  # None or True — default ~/.ssh/known_hosts
            kh = Path.home() / ".ssh" / "known_hosts"
            if kh.exists() and kh.stat().st_size > 0:
                _load_host_keys = getattr(client, "load_host_keys", None)
                if _load_host_keys:
                    _load_host_keys(str(kh))
                policy = paramiko.RejectPolicy()
            else:
                policy = paramiko.AutoAddPolicy()
        client.set_missing_host_key_policy(policy)

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

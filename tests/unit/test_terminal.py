"""Tests for TerminalBackend Protocol and implementations."""

import os
import pytest
from pathlib import Path
from microagent.terminal.backend import (
    TerminalResult, TerminalBackend,
    LocalTerminal, DockerTerminal,
)


class TestTerminalResult:
    def test_ok(self):
        r = TerminalResult.ok("hello", exit_code=0)
        assert r.success
        assert r.stdout == "hello"
        assert not r.is_timeout

    def test_timeout(self):
        r = TerminalResult.ok("partial", exit_code=-1, timed_out=True)
        assert not r.success
        assert r.is_timeout

    def test_error(self):
        r = TerminalResult.ok("error output", exit_code=1)
        assert not r.success


class TestLocalTerminal:
    async def test_run_echo(self):
        term = LocalTerminal()
        r = await term.run("echo hello")
        assert r.success
        assert "hello" in r.stdout

    async def test_run_with_cwd(self, tmp_path):
        term = LocalTerminal()
        r = await term.run("pwd", cwd=tmp_path)
        assert r.success
        assert str(tmp_path) in r.stdout

    async def test_run_timeout(self):
        term = LocalTerminal()
        r = await term.run("sleep 5", timeout=0.1)
        assert r.is_timeout

    async def test_run_failure(self):
        term = LocalTerminal()
        r = await term.run("exit 1")
        assert not r.success
        assert r.exit_code == 1


class TestDockerTerminal:
    @pytest.mark.skipif(
        not os.environ.get("MICROAGENT_DOCKER_TEST"),
        reason="Docker not available in CI",
    )
    async def test_run_echo_in_alpine(self):
        term = DockerTerminal(image="alpine:latest")
        r = await term.run("echo hello-from-docker")
        assert r.success
        assert "hello-from-docker" in r.stdout

    async def test_docker_not_available(self):
        """When docker is not running, returns error gracefully."""
        term = DockerTerminal(image="nonexistent-image:latest")
        r = await term.run("echo test", timeout=1.0)
        assert not r.success  # docker pull/run will fail

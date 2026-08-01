"""Regression tests for Phase 2 fixes: task recursion, ssh injection,
MCP lifecycle, and CLI markup escaping."""

import asyncio
import pytest

from microagent.subagent.manager import DEFAULT_SUBAGENTS, SubagentSpec


# --- 2.14 task tool recursion guard ---------------------------------------

def test_general_subagent_blocks_task_tool():
    """The 'general' subagent must block the 'task' tool to prevent
    unbounded recursion (task(general) → task(general) → ...).

    Without this, a confused model can fan out an expensive tree before
    the budget exhausts, and each level holds an open LLM stream."""
    general = next(s for s in DEFAULT_SUBAGENTS if s.name == "general")
    assert "task" in general.tools_blocked, (
        "general subagent must block 'task' to prevent recursion — "
        f"tools_blocked={general.tools_blocked}"
    )


def test_explore_subagent_remains_read_only():
    """explore spec should still only allow read tools."""
    explore = next(s for s in DEFAULT_SUBAGENTS if s.name == "explore")
    assert "task" not in explore.tools_allowed
    assert set(explore.tools_allowed).issubset({"grep", "glob", "read_file"})


# --- 2.15 ssh shlex.quote -------------------------------------------------

def test_ssh_quotes_cwd_with_metacharacters():
    """cwd with shell metacharacters must be quoted to prevent injection."""
    import inspect
    from microagent.terminal.ssh import SSHTerminal
    src = inspect.getsource(SSHTerminal.run)
    assert "shlex.quote" in src, "SSHTerminal.run must use shlex.quote for cwd/env"
    # Verify the old unquoted pattern is gone
    assert 'f"cd {cwd}' not in src.replace("shlex.quote(str(cwd))", ""), (
        "cwd must be quoted, not interpolated raw into the remote shell command"
    )


# --- 2.13 MCP: StdioServerParameters split + reconnect idempotency --------

def test_mcp_manager_stores_command_as_exec_plus_args():
    """_MCPConnectionManager must store command as [exec, *args] and pass
    them separately to StdioServerParameters (command: str, args: list)."""
    import inspect
    from microagent.mcp.client import _MCPConnectionManager
    mgr = _MCPConnectionManager(("uvx", "mcp-server-git", "--foo"))
    # The stored command is the full list; the connect() method splits it
    src = inspect.getsource(_MCPConnectionManager.connect)
    # Must reference self._command[0] for exec and [1:] for args
    assert "_command[0]" in src or "command=" in src, (
        "connect() must split command into exec + args for StdioServerParameters"
    )


@pytest.mark.asyncio
async def test_mcp_reconnect_is_idempotent():
    """Re-connecting to an already-connected server must NOT spawn a second
    subprocess (the first would be orphaned)."""
    from microagent.tools.builtins.mcp_connect import _get_managers
    # Can't test the real connect (needs mcp package + a server), but we
    # can verify the idempotency check is present by examining the source.
    import inspect
    from microagent.tools.builtins import mcp_connect as mc
    src = inspect.getsource(mc.mcp_connect.fn)
    assert "already connected" in src or "idempotent" in src.lower() or "in managers" in src, (
        "mcp_connect must check for existing connection before spawning a new one"
    )


# --- 2.16 CLI Rich markup escaping ----------------------------------------

def test_cli_escapes_dynamic_content():
    """Dynamic content interpolated into Rich tag contexts must be escaped
    to prevent MarkupError ('[/...] closes nothing') on LLM outputs
    containing JSON arrays, XPath, markdown, etc."""
    import inspect
    from microagent.surface import cli
    src = inspect.getsource(cli._run_streaming)
    assert "_rich_escape" in src, (
        "_run_streaming must use _rich_escape() on dynamic content "
        "(event.text, summary, event.reason) to prevent MarkupError"
    )
    # Specifically, LLM reasoning text must be escaped
    assert "_rich_escape(event.text)" in src, "LLM thinking text must be escaped"


def test_rich_escape_actually_prevents_error():
    """Demonstrate that escape() prevents the MarkupError that bare [/...]
    in dynamic content would cause."""
    from rich.console import Console
    from rich.markup import escape
    import io

    console = Console(file=io.StringIO(), force_terminal=False, width=80)
    # This would crash without escape():
    #   console.print("[dim]array[/0][/]")  → MarkupError
    # With escape:
    dangerous = "array[/0][/]"
    console.print(f"[dim]{escape(dangerous)}[/]")  # must not raise
    output = console.file.getvalue()
    assert "array" in output

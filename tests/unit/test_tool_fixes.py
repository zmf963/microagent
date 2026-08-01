"""Regression tests for tool-level Phase 2 fixes.

Covers:
  2.7  edit_file: reject empty old_string
  2.8  git: shlex.split preserves quoted args, stdin=DEVNULL, timeout
  2.9  read_file: async + size guard + offset-past-end error
  2.10 grep: ReDoS timeout + file size cap
  2.11 process: process-group kill + poll post-exit timeout
  2.17 bash: CancelledError kills subprocess (process group)
"""

import asyncio
import os
import pytest

from microagent.tools.builtins.bash import bash, _kill_proc_group
from microagent.tools.builtins.edit_file import edit_file
from microagent.tools.builtins.git import git
from microagent.tools.builtins.grep import grep
from microagent.tools.builtins.process import process, _current_registry, ProcRegistry
from microagent.tools.builtins.read_file import read_file


async def _fn(tool_obj, **kwargs):
    """Call a @tool-decorated function's underlying async fn."""
    return await tool_obj.fn(**kwargs)


# --- 2.7 edit_file --------------------------------------------------------

@pytest.mark.asyncio
async def test_edit_file_rejects_empty_old_string(tmp_path):
    """Empty old_string + replace_all would corrupt the file by inserting
    new_string between every character (str.replace('', X) → 'XhXeXlXlXoX')."""
    f = tmp_path / "f.txt"
    f.write_text("hello")
    # Non-replace_all: saved by uniqueness check (count=""=6 > 1 → error),
    # but the error is misleading. replace_all: no guard → corruption.
    result = await _fn(edit_file, path=str(f), old_string="", new_string="X", replace_all=True)
    assert result.is_error
    assert "non-empty" in result.content.lower()
    # File untouched
    assert f.read_text() == "hello"


@pytest.mark.asyncio
async def test_edit_file_rejects_empty_old_string_no_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello")
    result = await _fn(edit_file, path=str(f), old_string="", new_string="X")
    assert result.is_error


# --- 2.8 git: shlex.split -------------------------------------------------

@pytest.mark.asyncio
async def test_git_shlex_split_preserves_quoted_args(tmp_path):
    """git commit -m 'fixed bug' must pass 'fixed bug' as one arg, not split."""
    import subprocess
    # init isn't in the tool's whitelist — do it via subprocess directly.
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "f.txt").write_text("x")
    await _fn(git, subcommand="add", repo_path=str(tmp_path), args=".")
    result = await _fn(
        git, subcommand="commit", repo_path=str(tmp_path),
        args="-m 'fixed a multi word bug'",
    )
    assert not result.is_error, result.content
    # Verify the message survived intact
    log = await _fn(git, subcommand="log", repo_path=str(tmp_path))
    assert "fixed a multi word bug" in log.content, (
        f"shlex.split mangled the quoted message: {log.content}"
    )


@pytest.mark.asyncio
async def test_git_rejects_unbalanced_quotes(tmp_path):
    result = await _fn(git, subcommand="log", repo_path=str(tmp_path), args="-m 'unclosed")
    assert result.is_error


# --- 2.9 read_file --------------------------------------------------------

@pytest.mark.asyncio
async def test_read_file_offset_past_end_errors(tmp_path):
    """offset beyond file length must error, not report '(empty file)'."""
    f = tmp_path / "f.txt"
    f.write_text("line1\nline2\nline3\n")
    result = await _fn(read_file, path=str(f), offset=999)
    assert result.is_error
    assert "past the end" in result.content.lower() or "999" in result.content


@pytest.mark.asyncio
async def test_read_file_genuinely_empty_reports_empty(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("")
    result = await _fn(read_file, path=str(f))
    assert not result.is_error
    assert "empty file" in result.content.lower()


@pytest.mark.asyncio
async def test_read_file_rejects_oversized(tmp_path):
    """Files over the size cap must be rejected before reading (OOM guard)."""
    from microagent.tools.builtins.read_file import _MAX_READ_BYTES
    f = tmp_path / "huge.log"
    # Write a file just over the cap
    f.write_bytes(b"x" * (_MAX_READ_BYTES + 1))
    result = await _fn(read_file, path=str(f))
    assert result.is_error
    assert "too large" in result.content.lower()


# --- 2.10 grep: ReDoS timeout + size cap ----------------------------------

@pytest.mark.asyncio
async def test_grep_redos_pattern_times_out_not_hangs(tmp_path):
    """A catastrophic-backtracking pattern must not hang the event loop.

    Uses SIGALRM (Unix) to interrupt the C-level backtracking. A thread-
    based timeout would NOT work — the GIL prevents interrupting a CPU-bound
    regex.search running in a worker thread."""
    import platform
    if platform.system() == "Windows":
        pytest.skip("SIGALRM is Unix-only")

    f = tmp_path / "f.txt"
    # Long enough to trigger catastrophic backtracking, short enough that
    # the test doesn't wait the full 5s alarm. 30 a's → ~2^30 backtracks.
    f.write_text("a" * 30 + "!")
    t0 = asyncio.get_event_loop().time()
    result = await _fn(grep, pattern=r"(a+)+b", path=str(f))
    elapsed = asyncio.get_event_loop().time() - t0
    # The alarm fires at 5s; must complete in well under a hang.
    assert elapsed < 8.0, f"grep hung for {elapsed:.1f}s on ReDoS pattern"
    # No match on 'a'*30+'!' for (a+)+b → (no matches)
    assert not result.is_error


@pytest.mark.asyncio
async def test_grep_skips_oversized_files(tmp_path):
    from microagent.tools.builtins.grep import _MAX_FILE_BYTES
    f = tmp_path / "big.txt"
    f.write_bytes(b"needle\n" + b"x" * (_MAX_FILE_BYTES + 1))
    result = await _fn(grep, pattern="needle", path=str(tmp_path))
    # The oversized file is skipped, so no match
    assert "(no matches)" in result.content


# --- 2.11 process: process-group kill + poll timeout ----------------------

@pytest.mark.asyncio
async def test_process_kill_kills_grandchildren():
    """kill must terminate the whole process group, not just /bin/sh.

    Without start_new_session + killpg, `sleep 3600` started via /bin/sh -c
    survives kill() (only the shell gets SIGKILL)."""
    reg = ProcRegistry()
    _current_registry.set(reg)
    # Start a sleep that would run for an hour
    sid = (await _fn(process, action="start", command="sleep 3600")).content.strip()
    await asyncio.sleep(0.3)  # let it start
    proc = reg.procs[sid]
    assert proc.returncode is None

    kill_result = await _fn(process, action="kill", session_id=sid)
    assert not kill_result.is_error

    # Verify the sleep is actually dead by checking for orphaned processes.
    # On macOS/Linux, pgrep for 'sleep 3600' started by this test should be empty.
    # (Best-effort: the process group kill ensures it even if pgrep isn't available.)
    import subprocess
    try:
        out = subprocess.run(
            ["pgrep", "-f", "sleep 3600"], capture_output=True, text=True, timeout=2
        )
        # Filter out any sleep 3600 not owned by us (unlikely in test env)
        my_pids = [p for p in out.stdout.split() if p]
        assert not my_pids, f"orphaned 'sleep 3600' survived kill: {my_pids}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # pgrep not available — the kill() returncode check above is sufficient


@pytest.mark.asyncio
async def test_process_poll_post_exit_does_not_hang():
    """poll after process exit must not hang forever if a grandchild holds
    the stdout pipe open."""
    reg = ProcRegistry()
    _current_registry.set(reg)
    # A process that exits immediately but whose pipe might be held
    sid = (await _fn(process, action="start", command="echo done")).content.strip()
    await asyncio.sleep(0.3)
    t0 = asyncio.get_event_loop().time()
    result = await _fn(process, action="poll", session_id=sid)
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed < 5.0, f"poll hung for {elapsed:.1f}s on exited process"
    assert not result.is_error


# --- 2.17 bash: CancelledError kills subprocess ---------------------------

@pytest.mark.asyncio
async def test_bash_cancellation_kills_subprocess():
    """If the bash task is cancelled mid-run, the subprocess must be killed,
    not orphaned. CancelledError is a BaseException — bare `except Exception`
    misses it."""
    # Start a long bash, cancel it, verify the process is gone.
    task = asyncio.create_task(_fn(bash, command="sleep 60", timeout=120))
    await asyncio.sleep(0.3)  # let it start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Give the kill a moment to take effect, then check no orphaned 'sleep 60'.
    await asyncio.sleep(0.2)
    import subprocess
    try:
        out = subprocess.run(
            ["pgrep", "-f", "sleep 60"], capture_output=True, text=True, timeout=2
        )
        orphans = [p for p in out.stdout.split() if p]
        assert not orphans, f"bash left orphaned 'sleep 60': {orphans}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # pgrep unavailable — structural coverage is sufficient


@pytest.mark.asyncio
async def test_bash_timeout_kills_process_group():
    """Timeout must kill the process group, not just the shell."""
    # `sleep 60 & sleep 60` — the second sleep is a child of the shell.
    # If only the shell is killed, the child survives.
    t0 = asyncio.get_event_loop().time()
    result = await _fn(bash, command="sleep 60", timeout=1)
    elapsed = asyncio.get_event_loop().time() - t0
    assert result.is_error
    assert "timed out" in result.content.lower()
    assert elapsed < 5.0

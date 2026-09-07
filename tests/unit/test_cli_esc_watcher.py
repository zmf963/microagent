"""Round-22 regression tests for the CLI Esc-watcher stdin architecture.

Covers two fixes:
  * 🔴 executor starvation — the old watcher polled stdin via
    ``asyncio.to_thread(sys.stdin.read, 1)`` every 0.2s; each timed-out
    read leaves a default-executor worker blocked in an uninterruptible
    syscall, and once the pool fills every other ``to_thread`` caller
    (store writes, memory, file tools) deadlocks for the rest of the
    turn. The watcher now uses a dedicated daemon reader thread that
    select()s before reading.
  * 🟡 cbreak re-entry — after the question tool restores cooked mode the
    watcher must re-enter cbreak, or single-char reads (and thus Esc×2
    interrupts) never register again for the rest of the turn.
"""

import asyncio
import pty
import sys
import termios
import time
import tty

import pytest

from microagent.core.types import Message, TurnComplete
from microagent.surface import cli as cli_mod
from microagent.surface.cli import _run_streaming, console


class _StdinStub:
    """Redirect the watcher at a PTY slave so isatty()/termios/select all
    behave like a real terminal without needing an interactive test.

    ``read`` is a *blocking* single-char read on the slave fd — required so
    the OLD watcher implementation (asyncio.to_thread(sys.stdin.read, 1))
    genuinely blocks pool workers during the regression probe; without it
    the old watcher crashes on the stub instantly and the test can't tell
    the two implementations apart.
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def isatty(self) -> bool:  # pragma: no cover - trivial
        return True

    def fileno(self) -> int:
        return self._fd

    def read(self, n: int = -1):  # pragma: no cover - blocks by design
        import os

        return os.read(self._fd, n)


class _SlowRunner:
    """Fake runner whose single turn holds long enough for the OLD polling
    watcher to have piled up blocked executor workers."""

    def __init__(self, hold: float) -> None:
        self._hold = hold

    def interrupt(self) -> None:  # pragma: no cover - not triggered here
        pass

    async def run_turn(self, messages):  # noqa: ANN001
        await asyncio.sleep(self._hold)
        yield TurnComplete(content="done")


class _FakeAgent:
    def __init__(self, runner) -> None:  # noqa: ANN001
        self.runner = runner


class TestEscWatcherExecutorStarvation:
    @pytest.mark.skipif(
        not hasattr(sys, "stdin") or sys.platform == "win32",
        reason="requires POSIX termios/pty",
    )
    def test_watcher_does_not_starve_default_executor(self, monkeypatch):
        """While the watcher runs, parallel to_thread calls must still get
        workers promptly (the old code blocked them all on stdin reads)."""
        master, slave = pty.openpty()
        try:
            monkeypatch.setattr(sys, "stdin", _StdinStub(slave))

            async def _scenario() -> float:
                agent = _FakeAgent(_SlowRunner(hold=5.0))
                task = asyncio.create_task(
                    _run_streaming(agent, [Message.user("hi")])  # type: ignore[arg-type]
                )
                try:
                    # Let the (old-code) watcher pile up blocked readers:
                    # 3.5s × 5 polls/s ≈ 17 blocked workers — beyond the
                    # default pool size on typical dev machines.
                    await asyncio.sleep(3.5)
                    probes = [
                        asyncio.create_task(
                            asyncio.wait_for(
                                asyncio.to_thread(time.sleep, 0), timeout=2.0
                            )
                        )
                        for _ in range(12)
                    ]
                    t0 = time.monotonic()
                    await asyncio.gather(*probes)
                    return time.monotonic() - t0
                finally:
                    # Feed any blocked stdin readers (the OLD watcher's
                    # leaked executor workers) so asyncio.run's executor
                    # teardown can join them and the test process exits.
                    # Newlines matter: after the old watcher's finally
                    # restores cooked mode (ICANON on), a bare byte run is
                    # line-buffered and never delivered — one char per
                    # line is the only thing that unblocks os.read(1).
                    import os

                    try:
                        os.write(master, b"x\n" * 64)
                    except OSError:
                        pass
                    await asyncio.wait_for(task, timeout=8.0)

            with console.capture():
                elapsed = asyncio.run(_scenario())
            # All 12 probes got workers immediately (≪ timeout).
            assert elapsed < 1.5, (
                f"default executor starved: 12 to_thread probes took "
                f"{elapsed:.2f}s — the Esc watcher is blocking its workers"
            )
        finally:
            for fd in (master, slave):
                try:
                    import os

                    os.close(fd)
                except OSError:
                    pass


class TestEscWatcherCbreakReentry:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="requires POSIX termios/pty"
    )
    def test_cbreak_reapplied_after_question(self, monkeypatch):
        """After the question tool restores cooked mode and settles, the
        watcher must put the terminal back into cbreak (ICANON off) —
        otherwise Esc never registers again."""
        master, slave = pty.openpty()
        try:
            monkeypatch.setattr(sys, "stdin", _StdinStub(slave))
            from microagent.tools.builtins import question as q

            def _icanon_on() -> bool:
                attrs = termios.tcgetattr(slave)
                return bool(attrs[3] & termios.ICANON)

            async def _scenario() -> None:
                agent = _FakeAgent(_SlowRunner(hold=6.0))
                task = asyncio.create_task(
                    _run_streaming(agent, [Message.user("hi")])  # type: ignore[arg-type]
                )
                try:
                    # Wait for the watcher to enter cbreak.
                    for _ in range(40):
                        if not _icanon_on():
                            break
                        await asyncio.sleep(0.1)
                    assert not _icanon_on(), "watcher never entered cbreak"

                    # Simulate the question tool: pause the watcher,
                    # restore cooked mode, then settle.
                    q._QUESTION_ACTIVE.set()
                    await asyncio.sleep(0.3)  # watcher reaches pause loop
                    termios.tcsetattr(
                        slave, termios.TCSADRAIN, termios.tcgetattr(slave)
                    )
                    # Force cooked (ICANON on) exactly like _restore_cooked
                    # would with the published original settings.
                    attrs = termios.tcgetattr(slave)
                    attrs[3] |= termios.ICANON
                    termios.tcsetattr(slave, termios.TCSADRAIN, attrs)
                    assert _icanon_on()
                    q._QUESTION_ACTIVE.clear()

                    # Watcher should re-enter cbreak within its poll loop.
                    for _ in range(40):
                        if not _icanon_on():
                            break
                        await asyncio.sleep(0.1)
                    assert not _icanon_on(), (
                        "watcher did not re-enter cbreak after question — "
                        "Esc×2 interrupt permanently degraded"
                    )
                finally:
                    q._QUESTION_ACTIVE.clear()
                    await asyncio.wait_for(task, timeout=9.0)

            with console.capture():
                asyncio.run(_scenario())
        finally:
            for fd in (master, slave):
                try:
                    import os

                    os.close(fd)
                except OSError:
                    pass

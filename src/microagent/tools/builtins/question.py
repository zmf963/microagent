"""question builtin tool — ask the user a clarifying question.

When the LLM encounters ambiguity, it can ask the user for clarification
rather than guessing.  The tool blocks until the user responds (in
interactive surfaces) or returns an error (in non-interactive mode).
"""

from __future__ import annotations

import threading
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# Set while the question tool is waiting for input. The CLI's ESC watcher
# (surface/cli.py _watch_esc) polls this flag: while it is set the watcher
# pauses its single-char reads, so it cannot steal the user's keystrokes.
# threading.Event because the watcher checks it from the event loop thread
# while question's input() blocks in a worker thread — Event.set/clear/
# is_set are documented thread-safe.
_QUESTION_ACTIVE = threading.Event()

# The CLI publishes the terminal's original (cooked) termios settings here
# when its ESC watcher starts. The question tool restores cooked mode
# before calling input(): in the watcher's cbreak mode ICANON is off, so
# readline returns after a single keystroke and line editing is dead.
# Restoring cooked mode here (rather than waiting for the watcher to
# notice the flag) closes the window where the user's first characters
# would be truncated.
_ORIGINAL_TERMIOS = None  # list[termios attrs] set by the CLI, or None


def _restore_cooked() -> None:
    """Restore the terminal's cooked settings if the CLI published them.

    No-op when the CLI is not driving the terminal (library embedders,
    non-TTY stdin, or no termios on the platform) — in those cases the
    tty is already cooked and input() needs no help.
    """
    if _ORIGINAL_TERMIOS is None:
        return
    import sys

    if not sys.stdin.isatty():
        return
    try:
        import termios
    except ImportError:
        return
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _ORIGINAL_TERMIOS)
    except (OSError, termios.error):
        pass


@tool(
    "question",
    description="Ask the user a question when you need clarification. Returns the user's answer.",
)
async def question(
    text: Annotated[str, Field(description="The question to ask the user")],
    timeout: Annotated[
        int,
        Field(
            description="Max seconds to wait for user input (0 = no timeout)",
            ge=0,
            le=3600,
        ),
    ] = 300,
) -> ToolResult:
    """Ask the user a clarifying question.

    In interactive CLI/TUI mode, this blocks and waits for user input.
    In non-interactive/programmatic mode, returns an error indicating
    the question could not be answered.

    Has a configurable timeout (default 5 min) to prevent indefinite hangs.
    """
    import sys

    if not sys.stdin.isatty():
        return ToolResult.error(
            f"Cannot ask user — not running in interactive mode.\n"
            f"Question was: {text}\n"
            f"Please answer in your next message."
        )

    try:
        import asyncio

        _QUESTION_ACTIVE.set()
        # Restore cooked mode so input() gets proper line editing. The CLI's
        # ESC watcher puts the tty in cbreak mode (ICANON off) for single-
        # key Esc detection — in that mode readline returns after the first
        # keystroke, truncating answers to one character. The watcher pauses
        # while _QUESTION_ACTIVE is set, so restoring here is safe.
        _restore_cooked()
        print(f"\n❓ {text}")
        # input() is blocking — run off the event loop thread with timeout.
        # Note: if the tool call is cancelled (interrupt/budget), the await
        # raises promptly but the input() thread itself cannot be killed —
        # it lingers until the user presses Enter. Documented limitation
        # (Python cannot kill threads). Mitigated: the watcher pauses while
        # _QUESTION_ACTIVE is set so it does not steal keystrokes, and the
        # lingering thread exits harmlessly on the next Enter (input()
        # returns '' in cbreak mode, which the tool ignores).
        answer_future = asyncio.to_thread(input, "> ")
        try:
            if timeout > 0:
                answer = (await asyncio.wait_for(answer_future, timeout=timeout)).strip()
            else:
                answer = (await answer_future).strip()
        except asyncio.TimeoutError:
            return ToolResult.error(
                f"Question timed out after {timeout}s with no response.\n"
                f"(If the input line is still waiting, press Enter to clear it.)"
            )
        finally:
            _QUESTION_ACTIVE.clear()
        if not answer:
            return ToolResult.error("User provided no answer.")
        return ToolResult.ok(answer)
    except (EOFError, KeyboardInterrupt):
        _QUESTION_ACTIVE.clear()
        return ToolResult.error("User cancelled the question.")

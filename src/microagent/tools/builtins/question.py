"""question builtin tool — ask the user a clarifying question.

When the LLM encounters ambiguity, it can ask the user for clarification
rather than guessing.  The tool blocks until the user responds (in
interactive surfaces) or returns an error (in non-interactive mode).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


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

        print(f"\n❓ {text}")
        # input() is blocking — run off the event loop thread with timeout.
        # Note: if the tool call is cancelled (interrupt/budget), the await
        # raises promptly but the input() thread itself cannot be killed —
        # it lingers until the user presses Enter. Documented limitation.
        answer_future = asyncio.to_thread(input, "> ")
        if timeout > 0:
            answer = (await asyncio.wait_for(answer_future, timeout=timeout)).strip()
        else:
            answer = (await answer_future).strip()
        if not answer:
            return ToolResult.error("User provided no answer.")
        return ToolResult.ok(answer)
    except asyncio.TimeoutError:
        return ToolResult.error(f"Question timed out after {timeout}s with no response.")
    except (EOFError, KeyboardInterrupt):
        return ToolResult.error("User cancelled the question.")

"""CLI: REPL mode + one-shot mode with boxed tool calls and clean text.

Visual hierarchy:
  ╭─ 🔧 tool_name ─────────────────────────╮  ← cyan box for tool call
  │  args                                  │
  ╰─ ✓ result summary ────────────────────╯  ← green/red result line

  Clean text output flows below without markers.
"""

from __future__ import annotations

import shutil
import sys
import time

from ..agent import Agent
from ..config import Config
from ..core.types import (
    Message, TextDelta, ToolCallDelta, ToolResultDelta, TurnComplete, TurnFailed,
)

# ANSI
GRAY   = "\033[90m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RST    = "\033[0m"

# Box drawing
TITLE_PREFIX = f"{CYAN}🔧{RST} "


def _term_width() -> int:
    return shutil.get_terminal_size().columns


def main():
    cli_base_url = None; cli_api_key = None; cli_model = None; cli_system_prompt = None
    positional: list[str] = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--base-url" and i + 1 < len(args):
            cli_base_url = args[i + 1]; i += 2
        elif arg == "--api-key" and i + 1 < len(args):
            cli_api_key = args[i + 1]; i += 2
        elif arg == "--model" and i + 1 < len(args):
            cli_model = args[i + 1]; i += 2
        elif arg == "--system-prompt" and i + 1 < len(args):
            cli_system_prompt = args[i + 1]; i += 2
        elif arg in ("--help", "-h"):
            _print_help(); return
        else:
            positional.append(arg); i += 1

    config = Config.from_file(
        cli_base_url=cli_base_url, cli_api_key=cli_api_key,
        cli_model=cli_model, cli_system_prompt=cli_system_prompt,
    )
    if not config.llm.api_key:
        print("Warning: API key not set.", file=sys.stderr)

    agent = Agent.from_config(config.llm, system_prompt=config.system_prompt)
    session_id = f"cli-{int(time.time())}"

    if positional:
        prompt = " ".join(positional)
        _run_streaming(agent, [Message.user(prompt)])
        return

    print(f"{CYAN}{BOLD}MicroAgent v0.1.0{RST}  (model={config.llm.model})")
    print(f"Session: {session_id}")
    print("Type your message. Ctrl-D / Ctrl-C to exit.\n")

    messages: list[Message] = []
    while True:
        try:
            prompt = input(f"{BOLD}>>>{RST} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!"); break
        if not prompt:
            continue
        messages.append(Message.user(prompt))
        _run_streaming(agent, messages)
        print()


def _run_streaming(agent: Agent, messages: list[Message]) -> None:
    import asyncio

    async def _stream():
        text_started = False
        thinking_started = False
        pending_tool_call: tuple[str, dict] | None = None

        async for event in agent.runner.run_turn(messages):
            if isinstance(event, TextDelta):
                if event.kind == "thinking":
                    # Show thinking in gray italic, inline with content
                    if not thinking_started:
                        thinking_started = True
                        if text_started:
                            print()  # newline before thinking block
                        print(f"{GRAY}╭─ 💭 thinking ─{'─' * (min(_term_width() - 20, 50))}╮{RST}")
                    print(f"{GRAY}{event.text}{RST}", end="", flush=True)

                else:  # kind == "content"
                    if thinking_started and not text_started:
                        # Close thinking block, start content
                        print(f"\n{GRAY}╰{'─' * (min(_term_width() - 2, 60))}╯{RST}")
                        thinking_started = False
                    if not text_started:
                        text_started = True
                        if pending_tool_call:
                            print()  # blank line before text
                            pending_tool_call = None
                    print(event.text, end="", flush=True)

            elif isinstance(event, ToolCallDelta):
                # Open a tool box header
                width = min(_term_width() - 2, 78)
                title = f" {TITLE_PREFIX}{CYAN}{event.name}{RST} "
                args = _short_args(event.arguments)
                # Top border + title
                print(f"\n{GRAY}╭{RST}{title}{GRAY}{'─' * max(1, width - _display_len(title) - 1)}╮{RST}")
                # Args line
                print(f"{GRAY}│{RST} {GRAY}{args}{RST}")
                pending_tool_call = (event.name, event.arguments)

            elif isinstance(event, ToolResultDelta):
                width = min(_term_width() - 2, 78)
                summary = _summarize(event.content)
                if event.is_error:
                    mark = f"{RED}✗{RST}"
                else:
                    mark = f"{GREEN}✓{RST}"
                line = f" {mark} {GRAY}{summary}{RST}"
                # Bottom border + result
                print(f"{GRAY}╰{RST}{line}{' ' * max(1, width - _display_len(line) - 1)}{GRAY}╯{RST}")
                pending_tool_call = None

            elif isinstance(event, TurnComplete):
                if pending_tool_call:
                    print()  # close unclosed box
                if not text_started:
                    print(event.content)
                print()
                return

            elif isinstance(event, TurnFailed):
                if pending_tool_call:
                    print()
                print(f"{RED}✗{RST} {event.reason}")
                return

    asyncio.run(_stream())


def _display_len(s: str) -> int:
    """Visible length of a string (strip ANSI codes)."""
    import re
    return len(re.sub(r'\033\[[0-9;]*m', '', s))


def _short_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = str(v)
        if " " in s:
            s = f'"{s}"'
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(s)
    return " ".join(parts)


def _summarize(content: str) -> str:
    clean = content.replace("\n", " ").strip()
    if len(clean) > 70:
        clean = clean[:67] + "..."
    return clean


def _print_help():
    print("Usage: microagent [options] [prompt]")
    print()
    print("Options:")
    print("  --base-url URL        LLM API base URL")
    print("  --api-key KEY         API key")
    print("  --model MODEL         Model name")
    print("  --system-prompt TEXT  System prompt")
    print("  --help, -h            Show this help")
    print()
    print("Config file: ~/.microagent/config.yaml")
    print("Env vars: MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL")

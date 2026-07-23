"""CLI: REPL mode + one-shot mode with Hermes-style display.

Colors:
  \033[90m  — gray (┊ prefix, dim text)
  \033[96m  — cyan (tool names)
  \033[92m  — green (success)
  \033[91m  — red (error)
  \033[0m   — reset
"""

from __future__ import annotations

import sys
import time

from ..agent import Agent
from ..config import Config
from ..core.types import (
    Message, TextDelta, ToolCallDelta, ToolResultDelta, TurnComplete, TurnFailed,
)

# ANSI codes
GRAY  = "\033[90m"
CYAN  = "\033[96m"
GREEN = "\033[92m"
RED   = "\033[91m"
BOLD  = "\033[1m"
RST   = "\033[0m"
PREFIX = f"{GRAY}┊{RST}"


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
        had_tool_calls = False

        async for event in agent.runner.run_turn(messages):
            if isinstance(event, TextDelta):
                if not text_started:
                    text_started = True
                    if had_tool_calls:
                        print()  # blank line after tools
                print(event.text, end="", flush=True)

            elif isinstance(event, ToolCallDelta):
                had_tool_calls = True
                # Show tool call inline: ┊ name arg1 arg2
                args = _short_args(event.arguments)
                print(f"\n{PREFIX}{CYAN}{event.name}{RST} {GRAY}{args}{RST}")

            elif isinstance(event, ToolResultDelta):
                # Show result summary: ┊ ✓ result or ┊ ✗ error
                summary = _summarize(event.content)
                if event.is_error:
                    print(f"{PREFIX}{RED}✗{RST} {GRAY}{summary}{RST}")
                else:
                    print(f"{PREFIX}{GREEN}✓{RST} {GRAY}{summary}{RST}")

            elif isinstance(event, TurnComplete):
                if not text_started:
                    print(event.content)
                print()
                return

            elif isinstance(event, TurnFailed):
                print(f"\n{PREFIX}{RED}✗{RST} {event.reason}")
                return

    asyncio.run(_stream())


def _short_args(args: dict) -> str:
    """Format args as 'key=value' pairs, single-line."""
    parts = []
    for k, v in args.items():
        s = str(v)
        # Quote strings with spaces
        if " " in s:
            s = f'"{s}"'
        if len(s) > 50:
            s = s[:47] + "..."
        parts.append(s)
    return " ".join(parts)


def _summarize(content: str) -> str:
    """Single-line summary of tool result."""
    # Strip ANSI, take first line, truncate
    clean = content.replace("\n", " ").strip()
    if len(clean) > 80:
        clean = clean[:77] + "..."
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

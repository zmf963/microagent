"""Minimal CLI: REPL mode + one-shot mode with rich display.

Shows reasoning, tool calls, and tool results in real-time.
"""

from __future__ import annotations

import sys
import time

from ..agent import Agent
from ..config import Config
from ..core.types import Message, TextDelta, ToolCallDelta, TurnComplete, TurnFailed


def main():
    # Parse CLI args
    cli_base_url = None
    cli_api_key = None
    cli_model = None
    cli_system_prompt = None
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
            _print_help()
            return
        else:
            positional.append(arg); i += 1

    config = Config.from_file(
        cli_base_url=cli_base_url,
        cli_api_key=cli_api_key,
        cli_model=cli_model,
        cli_system_prompt=cli_system_prompt,
    )

    if not config.llm.api_key:
        print("Warning: API key not set.", file=sys.stderr)

    agent = Agent.from_config(config.llm, system_prompt=config.system_prompt)
    session_id = f"cli-{int(time.time())}"

    # One-shot mode
    if positional:
        prompt = " ".join(positional)
        messages: list[Message] = [Message.user(prompt)]
        _run_streaming(agent, messages)
        return

    # REPL mode
    print(f"\033[1;36mMicroAgent v0.1.0\033[0m  (model={config.llm.model})")
    print(f"Session: {session_id}")
    print("Type your message. Ctrl-D / Ctrl-C to exit.\n")

    messages: list[Message] = []
    while True:
        try:
            prompt = input("\033[1;32m>>>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not prompt:
            continue

        messages.append(Message.user(prompt))
        _run_streaming(agent, messages)
        print()


def _run_streaming(agent: Agent, messages: list[Message]) -> None:
    """Run a turn with real-time display of reasoning, tools, and text."""
    import asyncio

    async def _stream():
        tool_count = 0
        text_started = False
        async for event in agent.runner.run_turn(messages):
            if isinstance(event, TextDelta):
                if tool_count > 0:
                    print(f"\n\033[1;35m📝\033[0m ", end="")
                    tool_count = 0
                elif not text_started:
                    text_started = True
                    print(f"\033[1;35m🤔\033[0m ", end="")
                print(event.text, end="", flush=True)

            elif isinstance(event, ToolCallDelta):
                tool_count += 1
                print(f"\n\033[1;33m🔧 {event.name}\033[0m({_format_args(event.arguments)})")

            elif isinstance(event, TurnComplete):
                print(f"\n\033[1;32m✅\033[0m {event.content[:200]}{'...' if len(event.content) > 200 else ''}")
                return

            elif isinstance(event, TurnFailed):
                print(f"\n\033[1;31m❌\033[0m {event.reason}")
                return

    asyncio.run(_stream())


def _format_args(args: dict) -> str:
    """Format tool arguments for display, truncating long values."""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _print_help():
    print("Usage: microagent [options] [prompt]")
    print()
    print("Options:")
    print("  --base-url URL       LLM API base URL")
    print("  --api-key KEY        API key")
    print("  --model MODEL        Model name")
    print("  --system-prompt TEXT  System prompt")
    print("  --help, -h           Show this help")
    print()
    print("Config file: ~/.microagent/config.yaml")
    print("Env vars: MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL")

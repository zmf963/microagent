"""Minimal CLI: REPL mode + one-shot mode.

Usage:
    microagent                # → interactive REPL
    microagent "your prompt"  # → one-shot execution

Config resolution (highest priority first):
    1. CLI args:  --base-url, --api-key, --model, --system-prompt
    2. Env vars:  MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL, MICROAGENT_SYSTEM_PROMPT
    3. File:      ~/.microagent/config.yaml
    4. Defaults:  https://api.openai.com/v1, gpt-4o
"""

from __future__ import annotations

import sys
import time

from ..agent import Agent
from ..config import Config
from ..core.types import Message


def main():
    # Parse CLI args: handle both "--key value" and positional prompt
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
            print("Env vars: MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL, MICROAGENT_SYSTEM_PROMPT")
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
        print("Warning: API key not set. Set MICROAGENT_API_KEY env var or --api-key.", file=sys.stderr)

    agent = Agent.from_config(config.llm, system_prompt=config.system_prompt)
    session_id = f"cli-{int(time.time())}"

    # One-shot mode
    if positional:
        prompt = " ".join(positional)
        messages: list[Message] = [Message.user(prompt)]
        result = agent.run(messages)
        print(result)
        return

    # REPL mode
    print(f"MicroAgent v0.1.0  (model={config.llm.model}, base_url={config.llm.base_url})")
    print(f"Session: {session_id}")
    print("Type your message and press Enter. Ctrl-D / Ctrl-C to exit.\n")

    messages: list[Message] = []
    while True:
        try:
            prompt = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not prompt:
            continue

        messages.append(Message.user(prompt))
        result = agent.run(messages)
        print(result)
        print()

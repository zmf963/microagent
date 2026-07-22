"""Minimal CLI: REPL mode + one-shot mode.

Usage:
    microagent                # → interactive REPL
    microagent "your prompt"  # → one-shot execution
"""

from __future__ import annotations

import os
import sys
import time

from ..agent import Agent
from ..core.types import Message
from ..llm.client import LLMConfig


def main():
    # Read config from env
    base_url = os.environ.get("MICROAGENT_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("MICROAGENT_API_KEY", "")
    model = os.environ.get("MICROAGENT_MODEL", "gpt-4o")
    system_prompt = os.environ.get(
        "MICROAGENT_SYSTEM_PROMPT",
        "You are a helpful assistant. Use tools when appropriate."
    )

    if not api_key:
        print("Warning: MICROAGENT_API_KEY not set. Set it to use the LLM.", file=sys.stderr)

    config = LLMConfig(base_url=base_url, api_key=api_key, model=model)
    agent = Agent.from_config(config, system_prompt=system_prompt)
    session_id = f"cli-{int(time.time())}"

    # One-shot mode
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        messages: list[Message] = [Message.user(prompt)]
        result = agent.run(messages)
        print(result)
        return

    # REPL mode
    print(f"MicroAgent v0.1.0  (model={model}, session={session_id})")
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

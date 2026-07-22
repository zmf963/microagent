"""Minimal Textual TUI for MicroAgent.

Requires: pip install microagent[tui] (textual)

Usage: microagent-tui
"""

from __future__ import annotations

import os
import sys

from ..agent import Agent
from ..core.types import Message
from ..llm.client import LLMConfig


async def run_tui():
    try:
        from textual.app import App, ComposeResult
        from textual.containers import ScrollableContainer
        from textual.widgets import Header, Footer, Input, Static
    except ImportError:
        print("textual not installed. Install with: pip install microagent[tui]")
        sys.exit(1)

    base_url = os.environ.get("MICROAGENT_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("MICROAGENT_API_KEY", "")
    model = os.environ.get("MICROAGENT_MODEL", "gpt-4o")

    config = LLMConfig(base_url=base_url, api_key=api_key, model=model)
    agent = Agent.from_config(config)

    class MicroAgentTUI(App):
        CSS = """
        #chat { height: 1fr; border: solid green; }
        #input { dock: bottom; }
        """

        def compose(self) -> ComposeResult:
            yield Header()
            yield ScrollableContainer(Static("MicroAgent TUI\n", id="chat"))
            yield Input(placeholder="Type your message...", id="input")

        async def on_input_submitted(self, event: Input.Submitted):
            text = event.value.strip()
            if not text:
                return
            event.input.clear()
            chat = self.query_one("#chat", Static)
            chat.update(chat.renderable + f"\n>>> {text}\n")
            messages = [Message.user(text)]
            result = await agent.arun(messages)
            chat.update(chat.renderable + f"{result}\n")

    app = MicroAgentTUI()
    await app.run_async()


def main():
    import asyncio
    asyncio.run(run_tui())


if __name__ == "__main__":
    main()

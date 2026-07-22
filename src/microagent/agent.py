"""Agent — the one-stop facade for users.

Constructs all internal components from a Config and exposes a
simple ``run(session_id, prompt)`` / ``arun(session_id, prompt)`` API.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .core.types import Message, TurnComplete, TurnFailed
from .core.tool import ToolRegistry, _default_builtins
from .llm.client import LLMConfig, OpenAIChatClient
from .session.budget import Budget
from .session.runner import SessionRunner


@dataclass
class Agent:
    """One-stop facade: assembles all components internally."""
    runner: SessionRunner
    registry: ToolRegistry

    @classmethod
    def from_config(
        cls,
        llm_config: LLMConfig,
        *,
        system_prompt: str = "You are a helpful assistant.",
        max_iterations: int = 25,
        tools: list[Any] | None = None,
    ) -> Agent:
        # Build registry with default builtins + any extra tools
        all_tools = _default_builtins()
        if tools:
            all_tools.extend(tools)
        registry = ToolRegistry(all_tools)

        llm = OpenAIChatClient(llm_config)
        budget = Budget(max_iterations=max_iterations)
        runner = SessionRunner(
            llm=llm,
            registry=registry,
            budget=budget,
            system_prompt=system_prompt,
        )
        return cls(runner=runner, registry=registry)

    def run(self, messages: list[Message]) -> str:
        """Sync entry point: run a turn and return the final text."""
        return asyncio.run(self.arun(messages))

    async def arun(self, messages: list[Message]) -> str:
        """Async entry point: run a turn and return the final text."""
        async for event in self.runner.run_turn(messages):
            if isinstance(event, TurnComplete):
                return event.content
            if isinstance(event, TurnFailed):
                return f"[error: {event.reason}]"
        return "[error: turn ended without completion]"

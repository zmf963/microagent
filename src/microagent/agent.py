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
from .core.store import Store
from .session.budget import Budget
from .session.runner import SessionRunner


@dataclass
class Agent:
    """One-stop facade: assembles all components internally."""

    runner: SessionRunner
    registry: ToolRegistry
    cron: "object | None" = None  # CronScheduler, populated when enable_cron=True

    @classmethod
    def from_config(
        cls,
        llm_config: LLMConfig,
        *,
        system_prompt: str = "You are a helpful assistant.",
        max_iterations: int = 25,
        tools: list[Any] | None = None,
        store: "Store | None" = None,
        session_id: str = "default",
        enable_cron: bool = False,
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
            store=store,
            session_id=session_id,
        )
        agent = cls(runner=runner, registry=registry)

        if enable_cron:
            from .cron.scheduler import CronScheduler
            agent.cron = CronScheduler(agent=agent, store=store)

        return agent

    def run(self, text: str | list[Message]) -> str:
        """Sync entry point: accept a string (auto-wraps as user msg) or Message list."""
        if isinstance(text, str):
            text = [Message.user(text)]
        return asyncio.run(self.arun(text))

    async def arun(self, messages: list[Message]) -> str:
        """Async entry point: run a turn and return the final text."""
        async for event in self.runner.run_turn(messages):
            if isinstance(event, TurnComplete):
                return event.content
            if isinstance(event, TurnFailed):
                return f"[error: {event.reason}]"
        return "[error: turn ended without completion]"

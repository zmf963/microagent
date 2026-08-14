"""Agent — the one-stop facade for users.

Constructs all internal components from a Config and exposes a
simple ``run(session_id, prompt)`` / ``arun(session_id, prompt)`` API.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Any

from .core.store import Store
from .core.permission import PermissionEngine
from .core.tool import ToolRegistry, _default_builtins
from .core.types import Message, TurnComplete, TurnFailed
from .llm.client import LLMConfig, OpenAIChatClient
from .llm.templates import DEFAULT_TEMPLATE
from .session.budget import Budget
from .session.runner import SessionRunner
from .skill.loader import ClaudeSkillLoader


@dataclass
class Agent:
    """One-stop facade: assembles all components internally."""

    runner: SessionRunner
    registry: ToolRegistry
    cron: object | None = None  # CronScheduler, populated when enable_cron=True

    @classmethod
    def from_config(
        cls,
        llm_config: LLMConfig,
        *,
        system_prompt: str = DEFAULT_TEMPLATE,
        max_iterations: int = 25,
        tools: list[Any] | None = None,
        store: Store | None = None,
        session_id: str = "default",
        enable_cron: bool = False,
        skills_path: str | None = None,
        permission_engine: PermissionEngine | None = None,
        memory: Any | None | bool = True,
        memory_write_approval: bool = False,
    ) -> Agent:
        """Build an Agent.

        ``memory``: True (default) enables persistent cross-session memory
        — a SQLiteMemoryProvider at ~/.microagent/memory.db plus the
        LLM extractor, injected into every turn (Hermes-style default-on).
        Pass a MemoryProvider instance to use a custom backend, or False
        to disable. ``memory_write_approval`` (default False) routes
        extracted memories through the pending/approve gate — the CLI's
        /memory command drives it (Hermes write_approval semantics).
        """
        # Build registry with default builtins + any extra tools
        all_tools = _default_builtins()
        if tools:
            all_tools.extend(tools)
        registry = ToolRegistry(all_tools)

        # Build skill loader from built-in + user paths
        # Built-in skills ship under src/microagent/skills/ — always loaded.
        # User paths are colon-separated extras. Agent-created skills land
        # in ~/.microagent/skills (skill_manage's write dir) — ALWAYS in
        # the search path, so runtime-created skills are actually loadable
        # (before, they were written but the loader never searched there
        # unless the user manually configured skills_path).
        _builtin_skills = Path(__file__).resolve().parent / "skills"
        search_paths: list[Path] = [_builtin_skills] if _builtin_skills.is_dir() else []

        if skills_path:
            for p in skills_path.split(":"):
                p = p.strip()
                if p:
                    # Expand ~ so '~/.claude/skills' in skills_path resolves.
                    # (The loader's str→Path path also expands, but agent.py
                    # converts to Path here, so expand here to cover ~.)
                    search_paths.append(Path(p).expanduser())

        agent_skills_dir = Path.home() / ".microagent" / "skills"
        if agent_skills_dir not in search_paths:
            search_paths.append(agent_skills_dir)

        skill_loader = ClaudeSkillLoader(search_paths=tuple(search_paths)) if search_paths else None

        llm = OpenAIChatClient(llm_config)
        budget = Budget.root(max_iterations=max_iterations)

        # Memory: default-enabled (Hermes parity). True → construct the
        # default SQLite provider; instance → use as-is; False/None → off.
        memory_provider = None
        if memory is True:
            from .memory.provider import SQLiteMemoryProvider

            memory_provider = SQLiteMemoryProvider(
                Path.home() / ".microagent" / "memory.db"
            )
            memory_provider.write_approval = bool(memory_write_approval)
        elif memory is not None and memory is not False:
            memory_provider = memory

        runner = SessionRunner(
            llm=llm,
            registry=registry,
            budget=budget,
            system_prompt=system_prompt,
            store=store,
            session_id=session_id,
            skill_loader=skill_loader,
            permission_engine=permission_engine,
            memory=memory_provider,
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

        async def _run() -> str:
            try:
                return await self.arun(text)
            finally:
                await self.close()

        return asyncio.run(_run())

    async def arun(self, messages: list[Message]) -> str:
        """Async entry point: run a turn and return the final text.

        Caller is responsible for calling ``await agent.close()`` when done
        with the agent (releases browser pages, LLM clients, pending tasks).
        """
        async for event in self.runner.run_turn(messages):
            if isinstance(event, TurnComplete):
                return event.content
            if isinstance(event, TurnFailed):
                return f"[error: {event.reason}]"
        return "[error: turn ended without completion]"

    def steer(self, text: str) -> None:
        """Inject a steer text into the running turn.

        Delegates to SessionRunner.steer(). Schedules the async call
        on the running event loop — safe to call from a sync context.
        Errors in the async task are logged rather than silently lost.
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.runner.steer(text))

            def _on_done(t: "asyncio.Task") -> None:
                # t.exception() re-raises CancelledError for cancelled tasks —
                # the callback itself would then blow up with 'Exception in
                # callback' noise on loop shutdown.
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    logger.warning("steer task failed", exc_info=exc)

            task.add_done_callback(_on_done)
        except RuntimeError:
            # No running loop — create one temporarily
            asyncio.run(self.runner.steer(text))

    async def close(self) -> None:
        """Clean up all resources (cron, runner, LLM client, store)."""
        if self.cron is not None:
            await self.cron.stop()
        await self.runner.close()
        # Shut down the shared Chromium instance — it is a process-level
        # singleton, so it is closed here (Agent lifecycle) rather than in
        # runner.close(), which also runs for subagent child runners and
        # would kill this session's pages out from under it.
        from .tools.builtins.browser import close_global_browser

        await close_global_browser()
        # Prune expired tool-output files — cleanup_expired() had no caller,
        # so ~/.microagent/tool_outputs grew without bound despite the
        # documented 7-day retention.
        try:
            from .tools.output_store import ToolOutputStore

            await asyncio.to_thread(ToolOutputStore().cleanup_expired)
        except Exception:
            pass
        # Close the LLM client if it supports close()
        if hasattr(self.runner.llm, "close"):
            await self.runner.llm.close()
        # Close the store so SQLite connections / WAL files are released.
        # Library users who construct Agent directly (not via the CLI) would
        # otherwise leak a connection per agent. The CLI calls store.close()
        # itself too — sqlite3.Connection.close() is a documented no-op on a
        # second call, so the double-close is harmless.
        if self.runner.store is not None and hasattr(self.runner.store, "close"):
            self.runner.store.close()
        # Close the memory provider connection (Hermes parity: default-on
        # memory opens a SQLite connection per agent — release it).
        if self.runner.memory is not None and hasattr(self.runner.memory, "close"):
            try:
                self.runner.memory.close()
            except Exception:
                pass

    async def learn(
        self,
        source: str,
        *,
        kind: str = "chat",
    ) -> str:
        """Learn a reusable skill from a source (Hermes /learn parity).

        ``kind``:
          - "chat": ``source`` is conversation text (or "this conversation"
            is handled by the CLI, which passes the session history).
          - "dir": ``source`` is a directory path whose contents are
            distilled into a skill.
          - "url": ``source`` is a URL whose fetched text is distilled.

        Uses the auxiliary model when configured (cheaper), writes
        SKILL.md + .provenance.json (created_by="agent") + curator usage
        entry into ~/.microagent/skills — same write path as the
        skill_manage tool. Returns a status string.
        """
        from .skill.learner import learn_skill

        return await learn_skill(
            source=source,
            kind=kind,
            llm=self.runner.llm,
        )

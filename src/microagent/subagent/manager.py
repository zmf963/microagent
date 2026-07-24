"""Subagent system — spawn isolated child agents via the 'task' tool.

Subagents run with restricted toolsets and independent budgets.
The parent agent only sees the final text result — intermediate
tool calls and reasoning are invisible (context firewall).

Design (from design doc §9):
- SubagentSpec: declarative config for each subagent type
- SubagentManager: spawns subagents, filters tools, collects results
- M3b: child Budget spawned from parent (tree-shaped tracking + shared cancel)
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.tool import ToolRegistry
from ..core.types import Message, TurnComplete, TurnFailed
from ..session.runner import SessionRunner

# ---------------------------------------------------------------------------
# SubagentSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubagentSpec:
    """Declarative config for a subagent type."""

    name: str  # "explore" | "general"
    description: str
    system_prompt: str
    tools_allowed: tuple[str, ...]  # whitelist (empty = all)
    tools_blocked: tuple[str, ...]  # blacklist
    model: str | None = None  # None = inherit parent
    max_iterations: int = 10
    max_cost_usd: float = 1.0


# ---------------------------------------------------------------------------
# Default subagent specs
# ---------------------------------------------------------------------------

DEFAULT_SUBAGENTS: tuple[SubagentSpec, ...] = (
    SubagentSpec(
        name="explore",
        description="Read-only codebase exploration",
        system_prompt=(
            "You are a code explorer. Your job is to search code, read files, "
            "and return a concise answer. Do NOT write or edit any files."
        ),
        tools_allowed=("grep", "glob", "read_file"),
        tools_blocked=(),
        max_iterations=10,
        max_cost_usd=0.5,
    ),
    SubagentSpec(
        name="general",
        description="General multi-step task execution",
        system_prompt=(
            "You are a general-purpose subagent. Complete the task "
            "efficiently and return a clear result."
        ),
        tools_allowed=(),
        tools_blocked=("exit",),
        max_iterations=25,
        max_cost_usd=2.0,
    ),
)


# ---------------------------------------------------------------------------
# SubagentManager
# ---------------------------------------------------------------------------


class SubagentManager:
    """Manages subagent specs and spawns child agents."""

    def __init__(self, specs: tuple[SubagentSpec, ...] = DEFAULT_SUBAGENTS):
        self._specs = {s.name: s for s in specs}

    async def spawn(
        self,
        spec_name: str,
        prompt: str,
        parent_runner: SessionRunner,
    ) -> str:
        """Spawn a subagent and return its final text result.

        The subagent runs with:
        - Filtered tools (whitelist/blacklist from spec)
        - Child Budget spawned from parent (tree-shaped tracking + shared cancel)
        - Same LLM (model overridden via spec.model if set)
        """
        spec = self._specs[spec_name]

        # Build filtered tool registry
        child_registry = self._filter_registry(spec, parent_runner.registry)

        # Spawn child budget from parent — shares cancel_event,
        # reports consumption up the parent chain
        child_budget = parent_runner.budget.spawn(
            max_iterations=spec.max_iterations,
            max_cost_usd=spec.max_cost_usd,
        )

        # Build child runner — reuse parent LLM, optionally override model
        llm = parent_runner.llm
        forked_llm = False
        if spec.model:
            llm = llm.for_model(spec.model)
            forked_llm = True

        child_runner = SessionRunner(
            llm=llm,
            registry=child_registry,
            budget=child_budget,
            system_prompt=spec.system_prompt,
            event_bus=parent_runner.event_bus,
            pre_llm_hooks=parent_runner.pre_llm_hooks,
            tool_hooks=parent_runner.tool_hooks,
            context_sources=parent_runner.context_sources,
        )

        # Run the subagent turn
        messages: list[Message] = [Message.user(prompt)]
        parts: list[str] = []
        try:
            async for event in child_runner.run_turn(messages):
                if isinstance(event, TurnComplete):
                    parts.append(event.content)
                    break
                if isinstance(event, TurnFailed):
                    return f"[subagent {spec_name} failed: {event.reason}]"
        finally:
            await child_runner.close()
            # Close forked LLM client (has its own AsyncOpenAI connection pool)
            if forked_llm and hasattr(llm, "close"):
                await llm.close()

        return "".join(parts) or f"[subagent {spec_name} returned empty]"

    def _filter_registry(self, spec: SubagentSpec, parent_registry: ToolRegistry) -> ToolRegistry:
        """Build a filtered registry based on spec's allowlist/blocklist."""
        filtered = ToolRegistry()

        for name in parent_registry.names:
            # Blocklist takes priority
            if name in spec.tools_blocked:
                continue
            # If allowlist is set, only include those
            if spec.tools_allowed and name not in spec.tools_allowed:
                continue
            tool = parent_registry.get(name)
            if tool is not None:
                filtered.register(tool)

        return filtered

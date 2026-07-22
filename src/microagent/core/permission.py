"""Permission engine — rule-based tool access control.

Evaluates tool calls against a rule list (fnmatch on tool name +
argument constraints). Returns ALLOW / DENY / ASK decisions.

Design (from design doc §4):
- Rules are matched top-to-bottom; first match wins.
- Default policy: DENY (if no rule matches).
- ASK delegates to an ask_callback (CLI/Web injects one).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from typing import Any, Awaitable, Callable

from .types import ToolCall


# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------

class Decision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"   # interactive prompt (requires a Surface)


# ---------------------------------------------------------------------------
# Rule + PermissionDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Rule:
    tool_pattern: str            # fnmatch: "fs.*", "bash", "write_*"
    arguments_constraint: dict   # {"path": "/workspace/**"}, supports fnmatch
    decision: Decision
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    decision: Decision
    reason: str = ""

    @property
    def is_deny(self) -> bool:
        return self.decision is Decision.DENY


# Callback type for ASK decisions
AskCallback = Callable[[ToolCall, Rule], Awaitable[Decision]]


# ---------------------------------------------------------------------------
# PermissionEngine
# ---------------------------------------------------------------------------

class PermissionEngine:
    """Rule-based permission evaluator."""

    def __init__(
        self,
        rules: tuple[Rule, ...] = (),
        ask_callback: AskCallback | None = None,
    ):
        self.rules = rules
        self.ask_callback = ask_callback

    async def evaluate(
        self, call: ToolCall, ctx: Any = None
    ) -> PermissionDecision:
        """Evaluate a tool call against the rules."""
        for rule in self.rules:
            if not fnmatch(call.name, rule.tool_pattern):
                continue
            if not self._args_match(call.arguments, rule.arguments_constraint):
                continue
            if rule.decision is Decision.ASK and self.ask_callback:
                user_decision = await self.ask_callback(call, rule)
                return PermissionDecision(user_decision, rule.reason)
            return PermissionDecision(rule.decision, rule.reason)

        # Default deny (OpenCode same convention)
        return PermissionDecision(Decision.DENY, "no rule matched")

    def resolve(self, tool_name: str) -> Decision:
        """Quick resolve for materialize: returns the highest decision for a tool."""
        for rule in self.rules:
            if fnmatch(tool_name, rule.tool_pattern):
                return rule.decision
        return Decision.DENY

    @staticmethod
    def _args_match(args: dict, constraint: dict) -> bool:
        """Each key in constraint is fnmatch-matched against args' same-key string value."""
        for k, pat in constraint.items():
            val = args.get(k, "")
            if not isinstance(val, str) or not fnmatch(val, str(pat)):
                return False
        return True


# ---------------------------------------------------------------------------
# Default rules (from design doc §4.2)
# ---------------------------------------------------------------------------

DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("read_file", {}, Decision.ALLOW),
    Rule("grep", {}, Decision.ALLOW),
    Rule("glob", {}, Decision.ALLOW),
    Rule("write_file", {}, Decision.ALLOW),
    Rule("edit_file", {}, Decision.ALLOW),
    Rule("bash", {"command": "ls *"}, Decision.ALLOW),
    Rule("bash", {}, Decision.ALLOW),
    Rule("web_fetch", {}, Decision.ALLOW),
    Rule("todo", {}, Decision.ALLOW),
    Rule("plan", {}, Decision.ALLOW),
    Rule("exit", {}, Decision.ALLOW),
    Rule("task", {}, Decision.ALLOW),
    Rule("skill_manage", {}, Decision.ALLOW),
)

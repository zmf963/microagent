"""Permission engine — rule-based tool access control.

Evaluates tool calls against a rule list (fnmatch on tool name +
argument constraints). Returns ALLOW / DENY / ASK decisions.

Design (from design doc §4):
- Rules are matched top-to-bottom; first match wins.
- Default policy: DENY (if no rule matches).
- ASK delegates to an ask_callback (CLI/Web injects one).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from typing import Any

from .types import ToolCall

# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------


class Decision(Enum):
    """Tool permission decision: ALLOW, DENY, or ASK (interactive prompt)."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"  # interactive prompt (requires a Surface)


# ---------------------------------------------------------------------------
# Rule + PermissionDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """Permission rule: fnmatch tool name + argument constraints → decision."""

    tool_pattern: str  # fnmatch: "fs.*", "bash", "write_*"
    arguments_constraint: dict  # {"path": "/workspace/**"}, supports fnmatch
    decision: Decision
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """Result of evaluating a tool call — ALLOW/DENY/ASK + reason."""

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

    async def evaluate(self, call: ToolCall, ctx: Any = None) -> PermissionDecision:
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
        """Quick resolve for materialize: returns the most permissive decision for a tool.

        If any ALLOW rule matches, returns ALLOW.
        Otherwise returns the first matching rule's decision.
        This is used for tool list materialization (which tools to show LLM),
        not for actual permission evaluation (use evaluate() for that).
        """
        first_match = None
        for rule in self.rules:
            if fnmatch(tool_name, rule.tool_pattern):
                if first_match is None:
                    first_match = rule.decision
                if rule.decision is Decision.ALLOW:
                    return Decision.ALLOW
        if first_match is not None:
            return first_match
        return Decision.DENY

    @staticmethod
    def _args_match(args: dict, constraint: dict) -> bool:
        """Each key in constraint is fnmatch-matched against args' same-key value.

        Non-string values are converted to str before matching, so numeric
        and list-type arguments can be constrained too."""
        for k, pat in constraint.items():
            val = args.get(k, "")
            val_str = str(val) if not isinstance(val, str) else val
            if not fnmatch(val_str, str(pat)):
                return False
        return True


# ---------------------------------------------------------------------------
# ScriptRule — external script-based permissions
# ---------------------------------------------------------------------------


class ScriptRule:
    """Permission rule that delegates to an external Python script.

    The script receives a JSON object on stdin:
        {"name": "tool_name", "arguments": {...}, "id": "call_id"}
    It must print "allow" or "deny" to stdout and exit 0.
    Non-zero exit or timeout → denied.
    """

    def __init__(
        self,
        tool_pattern: str,
        arguments_constraint: dict | None = None,
        script: str = "",
        timeout: float = 5.0,
    ):
        self.tool_pattern = tool_pattern
        self.arguments_constraint = arguments_constraint or {}
        self.script = script
        self.timeout = timeout
        self.decision = Decision.ALLOW  # placeholder for compatibility
        self.reason = ""

    async def evaluate(self, call: ToolCall) -> PermissionDecision:
        """Run the external script and return its decision."""
        import asyncio
        import contextlib
        import json as _json

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                self.script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            payload = _json.dumps(
                {
                    "name": call.name,
                    "arguments": call.arguments,
                    "id": call.id,
                }
            )
            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(payload.encode()),
                    timeout=self.timeout,
                )
            except TimeoutError:
                with contextlib.suppress(Exception):
                    proc.kill()
                return PermissionDecision(Decision.DENY, "external script timeout")
            if proc.returncode != 0:
                return PermissionDecision(
                    Decision.DENY, f"external script exit code {proc.returncode}"
                )
            result = stdout.decode().strip().lower()
            if result == "allow":
                return PermissionDecision(Decision.ALLOW, "external script: allow")
            else:
                return PermissionDecision(Decision.DENY, f"external script: {result}")
        except Exception as e:
            return PermissionDecision(Decision.DENY, f"external script error: {e}")


DEFAULT_RULES: tuple[Rule, ...] = (
    # Sensitive operations — ASK before executing
    Rule("bash", {"command": "rm *"}, Decision.ASK, "rm command requires confirmation"),
    Rule("bash", {"command": "mv *"}, Decision.ASK, "mv command requires confirmation"),
    Rule("bash", {"command": "chmod *"}, Decision.ASK, "chmod command requires confirmation"),
    Rule("bash", {"command": "chown *"}, Decision.ASK, "chown command requires confirmation"),
    Rule("task", {}, Decision.ASK, "subagent spawn requires confirmation"),
    # Default: allow all tools
    Rule("read_file", {}, Decision.ALLOW),
    Rule("grep", {}, Decision.ALLOW),
    Rule("glob", {}, Decision.ALLOW),
    Rule("write_file", {}, Decision.ALLOW),
    Rule("edit_file", {}, Decision.ALLOW),
    Rule("bash", {}, Decision.ALLOW),
    Rule("web_fetch", {}, Decision.ALLOW),
    Rule("todo", {}, Decision.ALLOW),
    Rule("plan", {}, Decision.ALLOW),
    Rule("exit", {}, Decision.ALLOW),
    Rule("task", {}, Decision.ALLOW),
    Rule("skill_manage", {}, Decision.ALLOW),
    Rule("web_search", {}, Decision.ALLOW),
    Rule("execute_code", {}, Decision.ALLOW),
    Rule("vision_analyze", {}, Decision.ALLOW),
    Rule("browser_navigate", {}, Decision.ALLOW),
    Rule("browser_snapshot", {}, Decision.ALLOW),
    Rule("browser_click", {}, Decision.ALLOW),
    Rule("browser_type", {}, Decision.ALLOW),
    Rule("context7", {}, Decision.ALLOW),
    Rule("session_search", {}, Decision.ALLOW),
    Rule("process", {}, Decision.ALLOW),
    Rule("git", {}, Decision.ALLOW),
    Rule("file_tree", {}, Decision.ALLOW),
)

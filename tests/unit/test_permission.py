"""Tests for permission engine."""

from microagent.core.permission import (
    DEFAULT_RULES,
    Decision,
    PermissionEngine,
    Rule,
)
from microagent.core.types import ToolCall


class TestRule:
    def test_fnmatch_tool_name(self):
        rule = Rule("bash", {}, Decision.ALLOW)
        assert rule.tool_pattern == "bash"


class TestPermissionEngine:
    async def test_allow(self):
        engine = PermissionEngine(rules=(Rule("read_file", {}, Decision.ALLOW),))
        call = ToolCall(id="c1", name="read_file", arguments={})
        decision = await engine.evaluate(call)
        assert decision.decision is Decision.ALLOW

    async def test_deny_default(self):
        engine = PermissionEngine(rules=())
        call = ToolCall(id="c1", name="bash", arguments={})
        decision = await engine.evaluate(call)
        assert decision.is_deny
        assert "no rule" in decision.reason

    async def test_deny_explicit(self):
        engine = PermissionEngine(rules=(Rule("bash", {}, Decision.DENY, reason="blocked"),))
        call = ToolCall(id="c1", name="bash", arguments={})
        decision = await engine.evaluate(call)
        assert decision.is_deny
        assert "blocked" in decision.reason

    async def test_fnmatch_wildcard(self):
        engine = PermissionEngine(rules=(Rule("write_*", {}, Decision.ALLOW),))
        call = ToolCall(id="c1", name="write_file", arguments={})
        decision = await engine.evaluate(call)
        assert decision.decision is Decision.ALLOW

    async def test_first_match_wins(self):
        engine = PermissionEngine(
            rules=(
                Rule("bash", {"command": "ls *"}, Decision.ALLOW),
                Rule("bash", {}, Decision.DENY),
            )
        )
        call_ok = ToolCall(id="c1", name="bash", arguments={"command": "ls -la"})
        decision_ok = await engine.evaluate(call_ok)
        assert decision_ok.decision is Decision.ALLOW

        call_deny = ToolCall(id="c2", name="bash", arguments={"command": "rm -rf /"})
        decision_deny = await engine.evaluate(call_deny)
        assert decision_deny.is_deny

    async def test_ask_callback(self):
        results = []

        async def ask_cb(call, rule):
            results.append(call.name)
            return Decision.ALLOW

        engine = PermissionEngine(
            rules=(Rule("bash", {}, Decision.ASK),),
            ask_callback=ask_cb,
        )
        call = ToolCall(id="c1", name="bash", arguments={})
        decision = await engine.evaluate(call)
        assert decision.decision is Decision.ALLOW
        assert results == ["bash"]

    async def test_ask_without_callback_fails_closed(self):
        """ASK with no ask_callback must DENY — returning ASK let callers
        treat it as 'not denied' and silently execute sensitive ops."""
        engine = PermissionEngine(rules=(Rule("bash", {}, Decision.ASK),))
        call = ToolCall(id="c1", name="bash", arguments={"command": "rm *"})
        decision = await engine.evaluate(call)
        assert decision.is_deny
        assert "ask_callback" in (decision.reason or "")

    def test_resolve(self):
        engine = PermissionEngine(rules=(Rule("read_file", {}, Decision.ALLOW),))
        assert engine.resolve("read_file") is Decision.ALLOW
        assert engine.resolve("bash") is Decision.DENY

    def test_default_rules_cover_all_builtins(self):
        engine = PermissionEngine(DEFAULT_RULES)
        builtins = [
            "read_file",
            "bash",
            "write_file",
            "edit_file",
            "grep",
            "glob",
            "web_fetch",
            "web_search",
            "execute_code",
            "vision_analyze",
            "todo",
            "task_plan",
            "exit",
            "task",
            "skill_manage",
            "question",
            "lsp",
            "mcp_connect",
        ]
        for name in builtins:
            assert engine.resolve(name) is Decision.ALLOW, f"{name} not allowed"


class TestArgsMatchWhitespace:
    """Round-22 🔴 companion: fnmatch constraints must not be defeatable
    by surrounding whitespace — the shell runs " rm -rf /" exactly like
    "rm -rf /", so a leading-space command must still hit the "rm *"
    ASK rule instead of falling through to the bare bash ALLOW rule."""

    async def test_leading_whitespace_command_still_matches(self):
        engine = PermissionEngine(
            rules=(
                Rule("bash", {"command": "rm *"}, Decision.ASK, "rm needs confirm"),
                Rule("bash", {}, Decision.ALLOW),
            )
        )
        decision = await engine.evaluate(
            ToolCall(id="c1", name="bash", arguments={"command": " rm -rf /"})
        )
        # ASK without callback fails CLOSED (deny) — proving the rm rule
        # matched rather than falling through to ALLOW.
        assert decision.decision is Decision.DENY
        assert "ask_callback" in decision.reason.lower()

    async def test_trailing_whitespace_still_matches(self):
        engine = PermissionEngine(
            rules=(
                Rule("bash", {"command": "chmod *"}, Decision.DENY, "no chmod"),
                Rule("bash", {}, Decision.ALLOW),
            )
        )
        decision = await engine.evaluate(
            ToolCall(id="c1", name="bash", arguments={"command": "chmod 777 x\t"})
        )
        assert decision.decision is Decision.DENY

    async def test_nonmatching_command_still_allows(self):
        engine = PermissionEngine(
            rules=(
                Rule("bash", {"command": "rm *"}, Decision.DENY, "no rm"),
                Rule("bash", {}, Decision.ALLOW),
            )
        )
        decision = await engine.evaluate(
            ToolCall(id="c1", name="bash", arguments={"command": "ls -la"})
        )
        assert decision.decision is Decision.ALLOW

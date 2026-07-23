"""Tests for permission engine."""

import pytest
from microagent.core.permission import (
    PermissionEngine, Rule, Decision, DEFAULT_RULES,
)
from microagent.core.types import ToolCall


class TestRule:
    def test_fnmatch_tool_name(self):
        rule = Rule("bash", {}, Decision.ALLOW)
        assert rule.tool_pattern == "bash"


class TestPermissionEngine:
    async def test_allow(self):
        engine = PermissionEngine(rules=(
            Rule("read_file", {}, Decision.ALLOW),
        ))
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
        engine = PermissionEngine(rules=(
            Rule("bash", {}, Decision.DENY, reason="blocked"),
        ))
        call = ToolCall(id="c1", name="bash", arguments={})
        decision = await engine.evaluate(call)
        assert decision.is_deny
        assert "blocked" in decision.reason

    async def test_fnmatch_wildcard(self):
        engine = PermissionEngine(rules=(
            Rule("write_*", {}, Decision.ALLOW),
        ))
        call = ToolCall(id="c1", name="write_file", arguments={})
        decision = await engine.evaluate(call)
        assert decision.decision is Decision.ALLOW

    async def test_first_match_wins(self):
        engine = PermissionEngine(rules=(
            Rule("bash", {"command": "ls *"}, Decision.ALLOW),
            Rule("bash", {}, Decision.DENY),
        ))
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

    def test_resolve(self):
        engine = PermissionEngine(rules=(
            Rule("read_file", {}, Decision.ALLOW),
        ))
        assert engine.resolve("read_file") is Decision.ALLOW
        assert engine.resolve("bash") is Decision.DENY

    def test_default_rules_cover_all_builtins(self):
        engine = PermissionEngine(DEFAULT_RULES)
        builtins = [
            "read_file", "bash", "write_file", "edit_file",
            "grep", "glob", "web_fetch", "web_search", "execute_code", "todo", "plan", "exit", "task", "skill_manage",
        ]
        for name in builtins:
            assert engine.resolve(name) is Decision.ALLOW, f"{name} not allowed"

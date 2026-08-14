"""Extra coverage for session/runner.py: _plan_bash_violation unit
branches, _persist_user_tail error fallback, and permission_engine
DENY at execution time."""

import pytest

from microagent.core.permission import Decision, PermissionEngine, Rule
from microagent.core.tool import ToolRegistry
from microagent.core.types import Message, ToolCall, ToolResult, ToolResultDelta
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from tests.unit.fake_llm import FakeLLMClient, text_response, tool_response


class TestPlanBashViolationUnit:
    @pytest.mark.parametrize("cmd", [
        "/usr/bin/rm -rf /tmp/x",
        "cp a b",
        "mkdir -p x",
        "touch f",
        "echo hi > f",
        "echo hi >> f",
        "cat a >b",
        "git commit -m x",
        "git push",
        "sed -i s/a/b/ f",
        "sed -is/a/b/ f",
        "pip install requests",
        "npm uninstall express",
        "apt-get install curl",
        "brew upgrade wget",
        "echo ok | tee f",
        "kill 123",
        "git add file",
    ])
    def test_blocked(self, cmd):
        assert SessionRunner._plan_bash_violation(cmd) is not None

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "cat pyproject.toml",
        "grep -rn foo src/",
        "git status",
        "git log --oneline",
        "git diff HEAD",
        "sed s/a/b/ f",
        "pip list",
        "npm ls",
        "echo 'a>b'",
        "echo 'a >> b'",
        "wc -l f",
        "python --version",
    ])
    def test_allowed(self, cmd):
        assert SessionRunner._plan_bash_violation(cmd) is None

    @pytest.mark.parametrize("cmd", ["rm a; ls b", "ls a && rm b", "echo x || rm y", "cat a | rm b"])
    def test_segment_splitting(self, cmd):
        assert SessionRunner._plan_bash_violation(cmd) is not None

    @pytest.mark.parametrize("cmd", ["", "   ", "echo", "echo 'ok'"])
    def test_empty_segments_ok(self, cmd):
        assert SessionRunner._plan_bash_violation(cmd) is None

    def test_unclosed_quote_falls_back_to_split(self):
        assert SessionRunner._plan_bash_violation("rm 'unclosed") is not None


class TestPersistUserTailFallback:
    async def test_store_load_history_failure(self):
        """If the store's load_history raises, the user message is still
        appended and the turn completes."""

        class _BadLoadStore:
            async def load_history(self, session_id):
                raise RuntimeError("store down")

            async def append(self, session_id, msg):
                appended.append(msg)

            async def checkpoint(self, session_id):
                pass

            async def list_sessions(self):
                return []

            async def session_summaries(self):
                return []

        appended = []
        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=ToolRegistry(),
            budget=Budget.root(),
            store=_BadLoadStore(),
        )
        events = [e async for e in runner.run_turn([Message.user("hi")])]
        assert events and events[-1].__class__.__name__ == "TurnComplete"
        assert any(m.content == "hi" for m in appended)
        await runner.close()


class TestPermissionDenyAtExecution:
    async def test_deny_yields_denied_result(self):
        """A DENY decision from the permission engine surfaces as a
        denied ToolResultDelta — the tool function never runs."""
        engine = PermissionEngine(
            rules=(Rule("echo_text", {}, Decision.DENY, reason="policy says no"),)
        )

        from microagent.core.tool import tool

        executed = []

        @tool("echo_text", description="echo")
        async def echo_text(text: str) -> ToolResult:
            executed.append(text)
            return ToolResult.ok(text)

        fake = FakeLLMClient([
            tool_response([("c1", "echo_text", {"text": "x"})]),
            text_response("done"),
        ])
        runner = SessionRunner(
            llm=fake,
            registry=ToolRegistry([echo_text]),
            budget=Budget.root(),
            permission_engine=engine,
        )
        events = [e async for e in runner.run_turn([Message.user("go")])]
        results = [e for e in events if isinstance(e, ToolResultDelta)]
        assert len(results) == 1
        assert results[0].is_error
        assert "denied" in results[0].content.lower()
        assert "policy says no" in results[0].content
        assert executed == []
        await runner.close()

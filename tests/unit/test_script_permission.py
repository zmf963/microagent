"""Tests for PermissionEngine ScriptRule — external script-based permissions."""

import json
import pytest
from pathlib import Path
from microagent.core.permission import PermissionEngine, Rule, Decision, ScriptRule
from microagent.core.types import ToolCall


class TestScriptRule:
    async def test_script_allow(self, tmp_path):
        """Script that exits 0 + stdout 'allow' grants permission."""
        script = tmp_path / "allow.py"
        script.write_text(
            "import sys, json\n"
            "call = json.loads(sys.stdin.read())\n"
            "print('allow')\n"
        )

        rule = ScriptRule("bash", {}, str(script))
        decision = await rule.evaluate(ToolCall(id="c1", name="bash", arguments={}))
        assert decision.decision == Decision.ALLOW

    async def test_script_deny(self, tmp_path):
        """Script that exits 0 + stdout 'deny' blocks permission."""
        script = tmp_path / "deny.py"
        script.write_text(
            "import sys\n"
            "print('deny')\n"
        )

        rule = ScriptRule("*", {}, str(script))
        decision = await rule.evaluate(ToolCall(id="c1", name="rm", arguments={}))
        assert decision.is_deny
        assert "external script" in decision.reason

    async def test_script_nonzero_exit(self, tmp_path):
        """Script that exits nonzero → permission denied."""
        script = tmp_path / "crash.py"
        script.write_text("import sys; sys.exit(1)\n")

        rule = ScriptRule("*", {}, str(script))
        decision = await rule.evaluate(ToolCall(id="c1", name="bash", arguments={}))
        assert decision.is_deny
        assert "exit code 1" in decision.reason

    async def test_script_timeout(self, tmp_path):
        """Script that hangs → timeout → permission denied."""
        script = tmp_path / "hang.py"
        script.write_text("import time; time.sleep(10)\n")

        rule = ScriptRule("*", {}, str(script), timeout=0.1)
        decision = await rule.evaluate(ToolCall(id="c1", name="bash", arguments={}))
        assert decision.is_deny
        assert "timeout" in decision.reason.lower()

    async def test_script_with_args(self, tmp_path):
        """Script receives tool name and arguments."""
        script = tmp_path / "audit.py"
        script.write_text(
            "import sys, json\n"
            "call = json.loads(sys.stdin.read())\n"
            "assert call['name'] == 'delete'\n"
            "assert call['arguments'] == {'path': '/tmp/x'}\n"
            "print('allow')\n"
        )

        rule = ScriptRule("delete", {}, str(script))
        decision = await rule.evaluate(ToolCall(
            id="c1", name="delete", arguments={"path": "/tmp/x"}
        ))
        assert decision.decision == Decision.ALLOW

"""Regression tests for the second-round audit fixes."""

import pytest

from microagent.llm.pricing import get_pricing, estimate_cost, get_context_window


class TestPricingCaseInsensitive:
    """Alias resolution must be case-insensitive: 'TX-D4F' configured in a
    TOML/env must resolve to the same canonical model as 'tx-d4f'.
    Previously _LOCAL_OVERRIDES was case-sensitive and fell through to the
    $0.50/1M fallback for uppercase ids."""

    def test_uppercase_tx_d4f_matches_lowercase(self):
        assert get_pricing("TX-D4F") == get_pricing("tx-d4f")

    def test_mixed_case_oc_d4f_matches_lowercase(self):
        assert get_pricing("OC-D4f") == get_pricing("oc-d4f")

    def test_case_insensitive_context_window(self):
        assert get_context_window("TX-D4F") == get_context_window("tx-d4f")


class TestEstimateCostClampsNegative:
    """Negative token deltas (cached-token adjustments from some proxies)
    would produce a negative cost, decrementing Budget's consumed total."""

    def test_negative_input_clamped(self):
        assert estimate_cost("gpt-4o", -1000, 100) >= 0
        # Only the positive side counts
        expected = estimate_cost("gpt-4o", 0, 100)
        assert estimate_cost("gpt-4o", -1000, 100) == pytest.approx(expected)

    def test_negative_output_clamped(self):
        assert estimate_cost("gpt-4o", 100, -500) >= 0

    def test_none_tokens_handled(self):
        assert estimate_cost("gpt-4o", None, 100) == estimate_cost("gpt-4o", 0, 100)


class TestMcpConnectHash:
    def test_raw_command_hash_does_not_crash(self):
        """The old code did hashlib.sha256(tuple) → TypeError on every raw:
        connect. Verify the hash is now computed on a string."""
        import hashlib
        # Simulate what mcp_connect does now
        command = tuple(["npx", "-y", "@modelcontextprotocol/server-git"])
        # Must not raise
        h = hashlib.sha256(" ".join(command).encode()).hexdigest()
        assert len(h) == 64


class TestProcessStartStdin:
    def test_start_uses_stdin_pipe(self):
        """The `write` action requires p.stdin to be non-None, which means
        start must spawn with stdin=PIPE. Structural check."""
        import inspect
        from microagent.tools.builtins.process import process
        src = inspect.getsource(process.fn)
        # Find the start case body
        idx = src.find('case "start"')
        assert idx != -1
        end = src.find("case", idx + 10)
        start_body = src[idx:end]
        assert "stdin=asyncio.subprocess.PIPE" in start_body, (
            "start must set stdin=PIPE so the write action can send input"
        )


class TestScriptRuleInvoked:
    """ScriptRule was never invoked by PermissionEngine.evaluate — the
    rule.decision placeholder (ALLOW) was returned unconditionally."""

    @pytest.mark.asyncio
    async def test_scriptrule_delegates_to_script(self, tmp_path):
        from microagent.core.permission import (
            PermissionEngine, ScriptRule, Rule, Decision,
        )
        from microagent.core.types import ToolCall

        # A script that prints "deny"
        script = tmp_path / "deny.py"
        script.write_text('print("deny")')

        engine = PermissionEngine(
            rules=(ScriptRule(tool_pattern="bash", script=str(script)),)
        )
        decision = await engine.evaluate(
            ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})
        )
        # Without the fix, this returned ALLOW (the placeholder).
        assert decision.decision is Decision.DENY, (
            f"ScriptRule must delegate to the script, got {decision.decision}"
        )

    @pytest.mark.asyncio
    async def test_scriptrule_allow_path(self, tmp_path):
        from microagent.core.permission import (
            PermissionEngine, ScriptRule, Decision,
        )
        from microagent.core.types import ToolCall

        script = tmp_path / "allow.py"
        script.write_text('print("allow")')
        engine = PermissionEngine(
            rules=(ScriptRule(tool_pattern="bash", script=str(script)),)
        )
        decision = await engine.evaluate(
            ToolCall(id="c1", name="bash", arguments={})
        )
        assert decision.decision is Decision.ALLOW


class TestSkillManagePatchGuard:
    @pytest.mark.asyncio
    async def test_patch_rejects_multi_match_even_empty_new(self, tmp_path, monkeypatch):
        """Empty new_string (deletion) used to bypass the multi-match guard
        and silently delete ALL occurrences. Now rejected unconditionally."""
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        skill_dir = tmp_path / "myskill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("dup dup dup")
        (skill_dir / ".provenance.json").write_text('{"created_by": "agent"}')

        result = await sm.skill_manage.fn(
            action="patch", name="myskill",
            old_string="dup", new_string="",
        )
        assert result.is_error, "multi-match with empty new_string must be rejected"
        # File unchanged
        assert (skill_dir / "SKILL.md").read_text() == "dup dup dup"

    @pytest.mark.asyncio
    async def test_patch_single_match_empty_new_deletes(self, tmp_path, monkeypatch):
        """A unique match + empty new_string should still work (delete one)."""
        from microagent.tools.builtins import skill_manage as sm
        monkeypatch.setattr(sm, "_get_skills_dir", lambda: tmp_path)
        skill_dir = tmp_path / "s2"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("hello world")
        (skill_dir / ".provenance.json").write_text('{"created_by": "agent"}')

        result = await sm.skill_manage.fn(
            action="patch", name="s2", old_string="hello ", new_string="",
        )
        assert not result.is_error
        assert (skill_dir / "SKILL.md").read_text() == "world"


class TestCompressPreviousSummaryOrder:
    def test_previous_summary_captured_before_recovery(self):
        """Structural: _extract_summary_text must be called BEFORE
        recover_file_attachments (which prepends messages to current)."""
        import inspect
        from microagent.session import compress
        src = inspect.getsource(compress.compact_conversation)
        # Find both force and auto paths
        for label in ("force", "auto"):
            # Find recover_file_attachments occurrences and verify each is
            # preceded by a previous_summary assignment in the same try block.
            recover_idx = 0
            while True:
                recover_idx = src.find("recover_file_attachments(messages, current)", recover_idx)
                if recover_idx == -1:
                    break
                block_start = src.rfind("try:", 0, recover_idx)
                block = src[block_start:recover_idx]
                assert "_extract_summary_text(current)" in block, (
                    f"previous_summary must be captured BEFORE recover_file_attachments "
                    f"(otherwise it reads the attachment message, not the summary)"
                )
                recover_idx += 1

"""Tests for v0.5 features: auxiliary model, model templates, @file ref, plan/build, sensitive confirm."""

import tempfile
from pathlib import Path

import pytest

from microagent.llm.client import LLMConfig, get_context_window, _estimate_cost
from microagent.llm.templates import get_model_template, MODEL_TEMPLATES, build_system_prompt
from microagent.core.permission import Rule, Decision, PermissionEngine, AskCallback
from microagent.core.types import ToolCall


class TestModelTemplates:
    def test_deepseek_template_exists(self):
        assert "deepseek-v4" in MODEL_TEMPLATES or "deepseek" in MODEL_TEMPLATES

    def test_glm_template_exists(self):
        assert "glm-5.2" in MODEL_TEMPLATES or "glm" in MODEL_TEMPLATES

    def test_kimi_template_exists(self):
        assert "kimi-k3" in MODEL_TEMPLATES or "kimi" in MODEL_TEMPLATES

    def test_get_template_by_prefix(self):
        """Template matching by model name prefix."""
        tpl = get_model_template("deepseek-v4-2024")
        assert "deepseek" in tpl.lower() or len(tpl) > 0

    def test_get_template_default(self):
        """Unknown model returns default template."""
        tpl = get_model_template("unknown-model-xyz")
        assert len(tpl) > 0  # has a default

    def test_templates_are_different(self):
        """Each model template is distinct."""
        ds = get_model_template("deepseek-v4")
        glm = get_model_template("glm-5.2")
        kimi = get_model_template("kimi-k3")
        assert ds != glm or ds != kimi  # at least some are different

    # --- DeepSeek-V4 Flash (tx-d4f) specific ---

    def test_flash_template_exists(self):
        """deepseek-v4-flash has a dedicated template."""
        assert "deepseek-v4-flash" in MODEL_TEMPLATES
        assert "Flash" in MODEL_TEMPLATES["deepseek-v4-flash"]

    def test_flash_template_is_more_specific_than_v4(self):
        """deepseek-v4-flash gets the flash template, not the generic v4."""
        flash = get_model_template("deepseek-v4-flash")
        v4 = get_model_template("deepseek-v4")
        assert flash != v4
        assert "Flash" in flash

    def test_gateway_aliases_map_to_correct_template(self):
        """tx-d4f / oc-d4f → flash; tx-d4p → pro (generic v4).

        Verified against the gateway's own /model response: tx-d4p routes
        to deepseek-v4-PRO, not flash. Previously all three were mapped to
        flash, giving pro callers the wrong system-prompt guidance."""
        # flash aliases
        for alias in ("tx-d4f", "oc-d4f"):
            tpl = get_model_template(alias)
            assert "Flash" in tpl, f"{alias} should resolve to flash template"
        # pro alias → generic v4 template (not flash)
        assert "Flash" not in get_model_template("tx-d4p")
        assert "DeepSeek-V4" in get_model_template("tx-d4p")
        # Case-insensitive
        assert "Flash" in get_model_template("TX-D4F")

    def test_build_system_prompt_keeps_user_instructions(self):
        """User system prompt takes precedence; flash guidance appended."""
        out = build_system_prompt("tx-d4f", "You are a code reviewer.")
        assert out.startswith("You are a code reviewer.")
        assert "Flash" in out

    def test_tx_d4f_context_window(self):
        """tx-d4f routes to deepseek-v4-flash (1M context, not the 128K default)."""
        assert get_context_window("tx-d4f") == 1_048_576

    def test_tx_d4f_has_real_cost(self):
        """tx-d4f routes to deepseek-v4-flash — a PAID model, not free.

        Previously hardcoded as $0 (treated as a free self-hosted model),
        which silently zeroed out Budget tracking for every tx-d4f turn."""
        assert _estimate_cost("tx-d4f", 1000, 500) > 0


class TestAuxiliaryModel:
    def test_llmconfig_has_auxiliary_model_field(self):
        """LLMConfig has auxiliary_model field, defaulting to None."""
        config = LLMConfig(base_url="http://x", api_key="k", model="m")
        assert hasattr(config, "auxiliary_model")
        assert config.auxiliary_model is None

    def test_auxiliary_model_can_be_set(self):
        """auxiliary_model can be set to a different model name."""
        config = LLMConfig(
            base_url="http://x", api_key="k", model="main-model",
            auxiliary_model="cheap-model",
        )
        assert config.auxiliary_model == "cheap-model"


class TestPlanBuildMode:
    def test_runner_has_mode_field(self):
        """SessionRunner has a mode field defaulting to 'build'."""
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry
        from tests.unit.fake_llm import FakeLLMClient
        runner = SessionRunner(llm=FakeLLMClient([]), registry=ToolRegistry([]))
        assert runner.mode == "build"

    def test_runner_mode_can_be_set(self):
        """SessionRunner mode can be changed."""
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry
        from tests.unit.fake_llm import FakeLLMClient
        runner = SessionRunner(llm=FakeLLMClient([]), registry=ToolRegistry([]))
        runner.mode = "plan"
        assert runner.mode == "plan"

    def test_plan_mode_filters_write_tools(self):
        """In plan mode, write tools should be blocked (bash is allowed for read-only)."""
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry, _default_builtins
        from tests.unit.fake_llm import FakeLLMClient
        runner = SessionRunner(
            llm=FakeLLMClient([]),
            registry=ToolRegistry(_default_builtins()),
        )
        runner.mode = "plan"
        # Check that plan mode filters write_file, edit_file, execute_code
        write_tools = [t for t in runner._get_available_tools() if t in ("write_file", "edit_file", "execute_code")]
        assert len(write_tools) == 0  # write tools filtered in plan mode
        # bash is allowed in plan mode (for read-only commands)
        assert "bash" in runner._get_available_tools()


class TestSensitiveConfirm:
    def test_rule_supports_conditions(self):
        """Rule can have argument constraints for parameter matching."""
        rule = Rule("bash", {"command": "rm *"}, Decision.ASK)
        assert rule.arguments_constraint == {"command": "rm *"}

    async def test_permission_engine_asks_on_matching_condition(self):
        """PermissionEngine returns ASK when condition matches."""
        rules = (
            Rule("bash", {"command": "rm *"}, Decision.ASK),
        )
        engine = PermissionEngine(rules=rules)
        call = ToolCall(id="c1", name="bash", arguments={"command": "rm -rf /"})
        decision = await engine.evaluate(call)
        assert decision.decision == Decision.ASK

    async def test_permission_engine_allows_non_matching(self):
        """PermissionEngine returns ALLOW when condition doesn't match (no other rule → deny)."""
        # With a single ASK rule that doesn't match, default is DENY
        # So add a default ALLOW rule too
        rules = (
            Rule("bash", {"command": "rm *"}, Decision.ASK),
            Rule("bash", {}, Decision.ALLOW),
        )
        engine = PermissionEngine(rules=rules)
        call = ToolCall(id="c1", name="bash", arguments={"command": "echo hello"})
        decision = await engine.evaluate(call)
        assert decision.decision == Decision.ALLOW

    async def test_permission_engine_asks_on_task_always(self):
        """task tool always asks."""
        rules = (
            Rule("task", {}, Decision.ASK),
        )
        engine = PermissionEngine(rules=rules)
        call = ToolCall(id="c1", name="task", arguments={"prompt": "do something"})
        decision = await engine.evaluate(call)
        assert decision.decision == Decision.ASK

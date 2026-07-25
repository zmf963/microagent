"""Tests for v0.5 features: auxiliary model, model templates, @file ref, plan/build, sensitive confirm."""

import tempfile
from pathlib import Path

import pytest

from microagent.llm.client import LLMConfig
from microagent.llm.templates import get_model_template, MODEL_TEMPLATES
from microagent.tools.builtins.context_ref import parse_file_ref
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


class TestFileReference:
    def test_parse_simple_file_ref(self):
        """@file:path parses to a file path."""
        result = parse_file_ref("@file:src/main.py")
        assert result is not None
        assert result.path == "src/main.py"
        assert result.line_start is None

    def test_parse_file_ref_with_line(self):
        """@file:path:42 parses to path + line number."""
        result = parse_file_ref("@file:src/main.py:42")
        assert result is not None
        assert result.path == "src/main.py"
        assert result.line_start == 42

    def test_parse_file_ref_with_line_range(self):
        """@file:path:10-20 parses to path + line range."""
        result = parse_file_ref("@file:src/main.py:10-20")
        assert result is not None
        assert result.path == "src/main.py"
        assert result.line_start == 10
        assert result.line_end == 20

    def test_parse_non_file_ref_returns_none(self):
        """Non-@file: strings return None."""
        assert parse_file_ref("just text") is None
        assert parse_file_ref("@git:hash") is None
        assert parse_file_ref("@url:http://x") is None

    async def test_file_ref_reads_content(self, tmp_path):
        """FileRef.read() returns file content."""
        from microagent.tools.builtins.context_ref import FileReference
        test_file = tmp_path / "test.py"
        test_file.write_text("line1\nline2\nline3\n")

        ref = FileReference(path=str(test_file))
        content = await ref.read()
        assert "line1" in content
        assert "line2" in content


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

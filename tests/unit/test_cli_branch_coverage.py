"""CLI surface tests — _read_multiline, /memory, /learn, /models, and mode
commands, plus config/currency/permission branch coverage.

Deliberately extends (not duplicates) test_cli_commands.py: that file covers
/model, /history, /cost, /skill, /help, /plan, /build with a real runner.
These tests target the previously-uncovered branches using lightweight
SimpleNamespace fakes so failures isolate to the handler under test.
"""

import io
from types import SimpleNamespace

import pytest

import microagent.surface.cli as cli
from microagent.core.permission import (
    Decision,
    PermissionDecision,
    PermissionEngine,
    Rule,
    ScriptRule,
)
from microagent.core.types import Message, ToolCall, Usage
from microagent.memory.provider import Memory


# ---------------------------------------------------------------------------
# _read_multiline
# ---------------------------------------------------------------------------


class TestReadMultiline:
    def _patch_prompt(self, monkeypatch, replies):
        calls = []

        def fake_ask(*a, **k):
            calls.append(k.get("choices"))
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr(cli.Prompt, "ask", fake_ask)
        return calls

    def test_single_line(self, monkeypatch):
        self._patch_prompt(monkeypatch, ["hello world"])
        assert cli._read_multiline() == "hello world"

    def test_empty_input_returns_empty(self, monkeypatch):
        self._patch_prompt(monkeypatch, [""])
        assert cli._read_multiline() == ""

    def test_whitespace_only_returns_empty(self, monkeypatch):
        self._patch_prompt(monkeypatch, ["   "])
        assert cli._read_multiline() == ""

    def test_pasted_multiline_block(self, monkeypatch):
        replies = ["```python\nprint('hi')\n```", ""]
        self._patch_prompt(monkeypatch, replies)
        assert cli._read_multiline() == "```python\nprint('hi')\n```"

    def test_embedded_newlines_without_continuation_return_as_is(self, monkeypatch):
        # Continuation only triggers when the FIRST line ends with ``` or \;
        # a pasted block whose first chunk doesn't end that way returns as-is.
        replies = ["```python\nprint('hi')", "```", ""]
        self._patch_prompt(monkeypatch, replies)
        assert cli._read_multiline() == "```python\nprint('hi')"

    def test_block_ends_with_empty_line(self, monkeypatch):
        replies = ["```", "print('hi')", ""]
        self._patch_prompt(monkeypatch, replies)
        assert cli._read_multiline() == "```\nprint('hi')"

    def test_backslash_continuation_ends_with_empty_line(self, monkeypatch):
        replies = ["echo one \\", "echo two", ""]
        self._patch_prompt(monkeypatch, replies)
        assert cli._read_multiline() == "echo one \\\necho two"

    def test_eof_in_first_prompt_raises(self, monkeypatch):
        def fake_ask(*a, **k):
            raise EOFError

        monkeypatch.setattr(cli.Prompt, "ask", fake_ask)
        with pytest.raises(EOFError):
            cli._read_multiline()

    def test_eof_during_continuation_breaks(self, monkeypatch):
        replies = ["```", EOFError()]
        self._patch_prompt(monkeypatch, replies)
        assert cli._read_multiline() == "```"

    def test_continuation_lines_are_rstripped(self, monkeypatch):
        replies = ["```", "trailing space   ", ""]
        self._patch_prompt(monkeypatch, replies)
        assert cli._read_multiline() == "```\ntrailing space"


# ---------------------------------------------------------------------------
# fakes: agent / runner / provider / store
# ---------------------------------------------------------------------------


class FakeMemoryProvider:
    def __init__(self, pending=(), write_approval=True, error=None):
        self._pending = list(pending)
        self.write_approval = write_approval
        self.error = error
        self.approved = []
        self.rejected = []

    async def pending_memories(self):
        if self.error:
            raise self.error
        return tuple(self._pending)

    async def approve_memory(self, memory_id):
        if self.error:
            raise self.error
        self.approved.append(memory_id)

    async def reject_memory(self, memory_id):
        if self.error:
            raise self.error
        self.rejected.append(memory_id)


def make_runner(memory=None, model="fake-model"):
    from microagent.llm.client import LLMConfig

    llm = SimpleNamespace(config=LLMConfig("http://fake/v1", "fake-key", model))
    return SimpleNamespace(memory=memory, mode="build", llm=llm)


def make_state(monkeypatch, memory=None, runner=None, agent=None, config=None):
    """ReplState with SimpleNamespace agent/runner and a captured console.

    Captures console output in `state._buf` and restores the module-level
    console after the test (test_cli_commands.py uses the same trick).
    """
    runner = runner if runner is not None else make_runner(memory=memory)
    agent = agent if agent is not None else SimpleNamespace(learn=None, runner=runner)
    config = (
        config
        if config is not None
        else SimpleNamespace(
            llm=SimpleNamespace(model="fake-model"), system_prompt="test", skills_path=None
        )
    )
    if not hasattr(config.llm, "base_url"):
        from microagent.llm.client import LLMConfig

        config.llm = LLMConfig("http://fake/v1", "fake-key", config.llm.model)
    st = cli.ReplState(
        agent=agent,
        config=config,
        store=SimpleNamespace(),
        session_id="cli-test",
    )
    buf = io.StringIO()
    st._buf = buf
    monkeypatch.setattr(cli, "console", cli.Console(file=buf, force_terminal=False, width=100))
    return st


def _out(st):
    return st._buf.getvalue()


# ---------------------------------------------------------------------------
# /memory
# ---------------------------------------------------------------------------


class TestCmdMemory:
    @pytest.mark.asyncio
    async def test_memory_none(self, monkeypatch):
        st = make_state(monkeypatch, memory=None)
        await cli._cmd_memory(st, "")
        assert "memory is disabled" in _out(st)

    @pytest.mark.asyncio
    async def test_memory_none_still_prints_for_subcommand(self, monkeypatch):
        st = make_state(monkeypatch, memory=None)
        await cli._cmd_memory(st, "pending")
        assert "memory is disabled" in _out(st)

    @pytest.mark.asyncio
    async def test_status(self, monkeypatch):
        mem = FakeMemoryProvider(write_approval=True)
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "")
        out = _out(st)
        assert "enabled" in out and "yes" in out and "write_approval" in out

    @pytest.mark.asyncio
    async def test_status_approval_off(self, monkeypatch):
        mem = FakeMemoryProvider(write_approval=False)
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "")
        assert "write_approval" in _out(st)

    @pytest.mark.asyncio
    async def test_pending_approval_off(self, monkeypatch):
        mem = FakeMemoryProvider(write_approval=False)
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "pending")
        assert "write_approval is off" in _out(st)

    @pytest.mark.asyncio
    async def test_pending_empty(self, monkeypatch):
        mem = FakeMemoryProvider(pending=(), write_approval=True)
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "pending")
        assert "no pending memories" in _out(st)

    @pytest.mark.asyncio
    async def test_pending_lists_rows(self, monkeypatch):
        mem = FakeMemoryProvider(
            pending=(
                Memory(id="m1", content="user likes dark mode", category="preference", created_at=1.0),
                Memory(id="m2", content="project uses ruff", category="fact", created_at=2.0),
            ),
            write_approval=True,
        )
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "pending")
        out = _out(st)
        assert "m1" in out and "m2" in out
        assert "dark mode" in out and "preference" in out

    @pytest.mark.asyncio
    async def test_pending_error(self, monkeypatch):
        mem = FakeMemoryProvider(error=RuntimeError("db locked"))
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "pending")
        out = _out(st)
        assert "failed to list pending" in out
        assert "db locked" in out

    @pytest.mark.asyncio
    async def test_approve_missing_id(self, monkeypatch):
        mem = FakeMemoryProvider()
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "approve")
        assert "Usage: /memory approve" in _out(st)
        assert mem.approved == []

    @pytest.mark.asyncio
    async def test_approve_ok(self, monkeypatch):
        mem = FakeMemoryProvider()
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "approve m1")
        assert mem.approved == ["m1"]
        assert "approved" in _out(st)

    @pytest.mark.asyncio
    async def test_approve_error(self, monkeypatch):
        mem = FakeMemoryProvider(error=RuntimeError("not found"))
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "approve m1")
        out = _out(st)
        assert "approve failed" in out and "not found" in out

    @pytest.mark.asyncio
    async def test_reject_missing_id(self, monkeypatch):
        mem = FakeMemoryProvider()
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "reject")
        assert "Usage: /memory reject" in _out(st)
        assert mem.rejected == []

    @pytest.mark.asyncio
    async def test_reject_ok(self, monkeypatch):
        mem = FakeMemoryProvider()
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "reject m2")
        assert mem.rejected == ["m2"]
        assert "rejected" in _out(st)

    @pytest.mark.asyncio
    async def test_reject_error(self, monkeypatch):
        mem = FakeMemoryProvider(error=ValueError("bad id"))
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "reject m2")
        out = _out(st)
        assert "reject failed" in out and "bad id" in out

    @pytest.mark.asyncio
    async def test_unknown_subcommand_shows_status(self, monkeypatch):
        mem = FakeMemoryProvider(write_approval=True)
        st = make_state(monkeypatch, memory=mem)
        await cli._cmd_memory(st, "bogus")
        assert "write_approval" in _out(st)


# ---------------------------------------------------------------------------
# /learn
# ---------------------------------------------------------------------------


class TestCmdLearn:
    @pytest.mark.asyncio
    async def test_usage_error_bad_kind(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_learn(st, "wikipedia foo")
        assert "Usage: /learn" in _out(st)

    @pytest.mark.asyncio
    async def test_usage_error_no_arg(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_learn(st, "")
        assert "Usage: /learn" in _out(st)

    @pytest.mark.asyncio
    async def test_dir_missing_source(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_learn(st, "dir")
        assert "source is required" in _out(st)

    @pytest.mark.asyncio
    async def test_chat_missing_source_falls_through_to_history(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_learn(st, "chat")
        assert "no conversation history" in _out(st)

    @pytest.mark.asyncio
    async def test_chat_with_text(self, monkeypatch):
        calls = []

        async def fake_learn(source, kind):
            calls.append((source, kind))
            return "created skill: test"

        agent = SimpleNamespace(learn=fake_learn, runner=make_runner())
        st = make_state(monkeypatch, agent=agent)
        await cli._cmd_learn(st, "chat how to make coffee")
        assert calls == [("how to make coffee", "chat")]
        assert "created skill: test" in _out(st)

    @pytest.mark.asyncio
    async def test_chat_dot_with_history(self, monkeypatch):
        calls = []

        async def fake_learn(source, kind):
            calls.append((source, kind))
            return "created skill: from-history"

        agent = SimpleNamespace(learn=fake_learn, runner=make_runner())
        st = make_state(monkeypatch, agent=agent)
        st.messages = [Message.user("question one"), Message.assistant("answer one")]
        await cli._cmd_learn(st, "chat .")
        source, kind = calls[0]
        assert kind == "chat"
        assert "user: question one" in source
        assert "assistant: answer one" in source

    @pytest.mark.asyncio
    async def test_chat_dot_empty_history_error(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_learn(st, "chat .")
        assert "no conversation history" in _out(st)

    @pytest.mark.asyncio
    async def test_chat_empty_string_same_as_dot(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_learn(st, "chat ")
        assert "no conversation history" in _out(st)

    @pytest.mark.asyncio
    async def test_learn_exception_prints_error(self, monkeypatch):
        async def fake_learn(source, kind):
            raise RuntimeError("LLM down")

        agent = SimpleNamespace(learn=fake_learn, runner=make_runner())
        st = make_state(monkeypatch, agent=agent)
        await cli._cmd_learn(st, "dir /tmp")
        assert "learn failed" in _out(st)
        assert "LLM down" in _out(st)

    @pytest.mark.asyncio
    async def test_url_kind_passthrough(self, monkeypatch):
        calls = []

        async def fake_learn(source, kind):
            calls.append((source, kind))
            return "ok"

        agent = SimpleNamespace(learn=fake_learn, runner=make_runner())
        st = make_state(monkeypatch, agent=agent)
        await cli._cmd_learn(st, "url https://example.com")
        assert calls == [("https://example.com", "url")]


# ---------------------------------------------------------------------------
# /models
# ---------------------------------------------------------------------------


class TestCmdModels:
    @pytest.mark.asyncio
    async def test_count_path(self, monkeypatch):
        import microagent.llm.pricing as _pricing

        calls = []

        def fake_load_cache():
            calls.append(True)
            _pricing._cache = {"gpt-4o": {}, "claude": {}}
            return _pricing._cache

        monkeypatch.setattr(_pricing, "_load_cache", fake_load_cache)
        monkeypatch.setattr(_pricing, "_cache", {})
        st = make_state(monkeypatch)
        await cli._cmd_models(st, "count")
        assert calls == [True]
        assert "2 models in cache" in _out(st)

    @pytest.mark.asyncio
    async def test_refresh_path(self, monkeypatch):
        import microagent.llm.pricing as _pricing

        monkeypatch.setattr(_pricing, "refresh", lambda: 123)
        st = make_state(monkeypatch)
        await cli._cmd_models(st, "refresh")
        assert "123 models" in _out(st)

    @pytest.mark.asyncio
    async def test_lookup_path_with_explicit_model(self, monkeypatch):
        import microagent.llm.pricing as _pricing

        monkeypatch.setattr(_pricing, "get_pricing", lambda model: (2.5, 10.0))
        monkeypatch.setattr(_pricing, "get_context_window", lambda model: 128000)
        st = make_state(monkeypatch)
        await cli._cmd_models(st, "gpt-4o")
        out = _out(st)
        assert "gpt-4o" in out
        assert "128,000" in out
        assert "¥18.0000/1M" in out  # 2.5 * 7.2 default rate
        assert "¥72.0000/1M" in out  # 10.0 * 7.2

    @pytest.mark.asyncio
    async def test_lookup_path_defaults_to_current_model(self, monkeypatch):
        import microagent.llm.pricing as _pricing

        seen = []

        def fake_get_pricing(model):
            seen.append(model)
            return (1.0, 1.0)

        monkeypatch.setattr(_pricing, "get_pricing", fake_get_pricing)
        monkeypatch.setattr(_pricing, "get_context_window", lambda model: 8192)
        st = make_state(monkeypatch)
        await cli._cmd_models(st, "")
        assert seen == ["fake-model"]


# ---------------------------------------------------------------------------
# smoke: /cost /history /clear + mode toggles
# ---------------------------------------------------------------------------


class TestCmdSmoke:
    @pytest.mark.asyncio
    async def test_cmd_cost(self, monkeypatch):
        st = make_state(monkeypatch)
        st.usage_tracker.record(Usage(input_tokens=100, output_tokens=50, cost_usd=0.01))
        await cli._cmd_cost(st, "")
        assert "Tokens: 100 in / 50 out" in _out(st)

    @pytest.mark.asyncio
    async def test_cmd_history_empty(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_history(st, "")
        assert "no messages" in _out(st)

    @pytest.mark.asyncio
    async def test_cmd_history_with_messages(self, monkeypatch):
        st = make_state(monkeypatch)
        st.messages = [Message.user("hello"), Message.assistant("world")]
        await cli._cmd_history(st, "")
        out = _out(st)
        assert "hello" in out and "world" in out

    @pytest.mark.asyncio
    async def test_cmd_clear(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_clear(st, "")

    @pytest.mark.asyncio
    async def test_cmd_plan_sets_runner_mode(self, monkeypatch):
        st = make_state(monkeypatch)
        st.agent.runner.mode = "build"
        await cli._cmd_plan(st, "")
        assert st.agent.runner.mode == "plan"

    @pytest.mark.asyncio
    async def test_cmd_build_sets_runner_mode(self, monkeypatch):
        st = make_state(monkeypatch)
        st.agent.runner.mode = "plan"
        await cli._cmd_build(st, "")
        assert st.agent.runner.mode == "build"

    @pytest.mark.asyncio
    async def test_cmd_thinking_toggle(self, monkeypatch):
        st = make_state(monkeypatch)
        assert st.show_thinking is False
        await cli._cmd_thinking(st, "")
        assert st.show_thinking is True
        await cli._cmd_thinking(st, "")
        assert st.show_thinking is False

    @pytest.mark.asyncio
    async def test_cmd_thinking_on(self, monkeypatch):
        st = make_state(monkeypatch)
        await cli._cmd_thinking(st, "on")
        assert st.show_thinking is True
        assert "shown" in _out(st)

    @pytest.mark.asyncio
    async def test_cmd_thinking_off(self, monkeypatch):
        st = make_state(monkeypatch)
        st.show_thinking = True
        await cli._cmd_thinking(st, "off")
        assert st.show_thinking is False
        assert "hidden" in _out(st)

    @pytest.mark.asyncio
    async def test_cmd_thinking_truthy_variants(self, monkeypatch):
        st = make_state(monkeypatch)
        for v in ("on", "true", "yes", "1"):
            await cli._cmd_thinking(st, v)
            assert st.show_thinking is True

    @pytest.mark.asyncio
    async def test_cmd_thinking_falsy_variants(self, monkeypatch):
        st = make_state(monkeypatch)
        st.show_thinking = True
        for v in ("off", "false", "no", "0"):
            await cli._cmd_thinking(st, v)
            assert st.show_thinking is False


# ---------------------------------------------------------------------------
# permission.py — resolve() and ScriptRule inside PermissionEngine
# ---------------------------------------------------------------------------


class TestResolve:
    def test_no_rules_denies(self):
        assert PermissionEngine(()).resolve("bash") is Decision.DENY

    def test_first_matching_rule_wins_without_allow(self):
        engine = PermissionEngine((Rule("bash", {}, Decision.DENY),))
        assert engine.resolve("bash") is Decision.DENY

    def test_allow_anywhere_wins_over_earlier_deny(self):
        engine = PermissionEngine(
            (Rule("bash", {}, Decision.DENY), Rule("b*", {}, Decision.ALLOW))
        )
        assert engine.resolve("bash") is Decision.ALLOW

    def test_unmatched_tool_denied(self):
        engine = PermissionEngine((Rule("read_file", {}, Decision.ALLOW),))
        assert engine.resolve("bash") is Decision.DENY


class TestArgsMatch:
    def test_matching_values(self):
        engine = PermissionEngine(
            (Rule("bash", {"command": "ls *"}, Decision.ALLOW),)
        )
        call = ToolCall(id="c1", name="bash", arguments={"command": "ls -la"})
        assert engine._args_match(call.arguments, {"command": "ls *"})

    def test_non_matching_value(self):
        engine = PermissionEngine(())
        assert not engine._args_match({"command": "rm -rf /"}, {"command": "ls *"})

    def test_missing_key_falls_back_to_empty_string(self):
        engine = PermissionEngine(())
        assert engine._args_match({}, {"command": "*"})
        assert not engine._args_match({}, {"command": "ls *"})

    def test_non_string_values_coerced(self):
        engine = PermissionEngine(())
        assert engine._args_match({"count": 5}, {"count": "5"})
        assert engine._args_match({"count": 5}, {"count": "*"})

    def test_empty_constraint_always_matches(self):
        engine = PermissionEngine(())
        assert engine._args_match({}, {})


class TestScriptRuleThroughEngine:
    async def test_script_rule_engine_delegates_allow(self, tmp_path):
        script = tmp_path / "ok.py"
        script.write_text("import sys, json\njson.loads(sys.stdin.read())\nprint('allow')\n")
        engine = PermissionEngine((ScriptRule("bash", {}, str(script)),))
        decision = await engine.evaluate(ToolCall(id="c1", name="bash", arguments={}))
        assert decision.decision is Decision.ALLOW
        assert "external script" in decision.reason

    async def test_script_rule_engine_delegates_deny(self, tmp_path):
        script = tmp_path / "no.py"
        script.write_text("import sys\nprint('deny')\n")
        engine = PermissionEngine((ScriptRule("bash", {}, str(script)),))
        decision = await engine.evaluate(ToolCall(id="c1", name="bash", arguments={}))
        assert decision.is_deny
        assert "external script" in decision.reason

    async def test_script_rule_engine_respects_args_constraint(self, tmp_path):
        script = tmp_path / "audit.py"
        script.write_text("import sys\nprint('allow')\n")
        engine = PermissionEngine(
            (ScriptRule("bash", {"command": "safe *"}, str(script)), Rule("bash", {}, Decision.DENY))
        )
        ok = await engine.evaluate(
            ToolCall(id="c1", name="bash", arguments={"command": "safe ls"})
        )
        assert ok.decision is Decision.ALLOW
        deny = await engine.evaluate(
            ToolCall(id="c2", name="bash", arguments={"command": "rm -rf /"})
        )
        assert deny.is_deny


class TestDecisionEnum:
    def test_values(self):
        assert Decision.ALLOW.value == "allow"
        assert Decision.DENY.value == "deny"
        assert Decision.ASK.value == "ask"


class TestPermissionDecision:
    def test_is_deny_property(self):
        assert PermissionDecision(Decision.DENY).is_deny
        assert not PermissionDecision(Decision.ALLOW).is_deny
        assert not PermissionDecision(Decision.ASK).is_deny


class TestEvaluateAskBranches:
    async def test_ask_callback_deny_result(self):
        async def ask_cb(call, rule):
            return Decision.DENY

        engine = PermissionEngine(
            rules=(Rule("bash", {}, Decision.ASK, reason="needs confirmation"),),
            ask_callback=ask_cb,
        )
        decision = await engine.evaluate(ToolCall(id="c1", name="bash", arguments={}))
        assert decision.is_deny
        assert decision.reason == "needs confirmation"

    async def test_ask_callback_result_replaces_reason(self):
        async def ask_cb(call, rule):
            return Decision.ALLOW

        engine = PermissionEngine(
            rules=(Rule("bash", {}, Decision.ASK, reason="why"),),
            ask_callback=ask_cb,
        )
        decision = await engine.evaluate(ToolCall(id="c1", name="bash", arguments={}))
        assert decision.decision is Decision.ALLOW
        assert decision.reason == "why"

    async def test_no_rule_match_default_deny_reason(self):
        decision = await PermissionEngine(()).evaluate(
            ToolCall(id="c1", name="unknown_tool", arguments={})
        )
        assert decision.is_deny
        assert decision.reason == "no rule matched"


# ---------------------------------------------------------------------------
# config.py — env / malformed file branches (complements test_config.py)
# ---------------------------------------------------------------------------


class TestConfigEnvAndMalformed:
    def test_env_overrides_file_skills_and_prompt(self, tmp_path, monkeypatch):
        from microagent.config import Config

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "model:\n  model: file-model\nsystem_prompt: file prompt\nskills_path: /file/skills\n"
        )
        monkeypatch.setattr(Config, "_config_path", lambda: config_file)
        monkeypatch.setenv("MICROAGENT_SYSTEM_PROMPT", "env prompt")
        monkeypatch.setenv("MICROAGENT_SKILLS_PATH", "/env/skills")
        monkeypatch.setenv("MICROAGENT_API_KEY", "env-key")

        cfg = Config.from_file()
        assert cfg.system_prompt == "env prompt"
        assert cfg.skills_path == "/env/skills"
        assert cfg.llm.api_key == "env-key"
        assert cfg.llm.model == "file-model"

    def test_cli_overrides_env(self, tmp_path, monkeypatch):
        from microagent.config import Config

        monkeypatch.setattr(Config, "_config_path", lambda: tmp_path / "missing.yaml")
        monkeypatch.setenv("MICROAGENT_MODEL", "env-model")
        monkeypatch.setenv("MICROAGENT_API_KEY", "env-key")
        monkeypatch.setenv("MICROAGENT_SKILLS_PATH", "/env/skills")

        cfg = Config.from_file(
            cli_model="cli-model",
            cli_api_key="cli-key",
            cli_skills_path="/cli/skills",
        )
        assert cfg.llm.model == "cli-model"
        assert cfg.llm.api_key == "cli-key"
        assert cfg.skills_path == "/cli/skills"

    def test_malformed_yaml_falls_back_with_warning(self, tmp_path, monkeypatch, caplog):
        from microagent.config import Config

        config_file = tmp_path / "config.yaml"
        config_file.write_text("{ not valid yaml !!\n")
        monkeypatch.setattr(Config, "_config_path", lambda: config_file)

        with caplog.at_level("WARNING"):
            cfg = Config.from_file()
        assert cfg.llm.model == "gpt-4o"
        assert any("Failed to read config file" in r.message for r in caplog.records)

    def test_model_section_non_mapping_falls_back(self, tmp_path, monkeypatch):
        from microagent.config import Config

        config_file = tmp_path / "config.yaml"
        config_file.write_text("model: just-a-string\nsystem_prompt: still works\n")
        monkeypatch.setattr(Config, "_config_path", lambda: config_file)

        cfg = Config.from_file()
        assert cfg.llm.model == "gpt-4o"
        assert cfg.system_prompt == "still works"

    def test_missing_file_uses_env_values(self, tmp_path, monkeypatch):
        from microagent.config import Config

        monkeypatch.setattr(Config, "_config_path", lambda: tmp_path / "missing.yaml")
        monkeypatch.setenv("MICROAGENT_BASE_URL", "http://env/v1")
        monkeypatch.setenv("MICROAGENT_API_KEY", "sk-env")

        cfg = Config.from_file()
        assert cfg.llm.base_url == "http://env/v1"
        assert cfg.llm.api_key == "sk-env"
        assert cfg.llm.model == "gpt-4o"


# ---------------------------------------------------------------------------
# sessions: _list_sessions, _pick_last_session, /list, /resume branches
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self, summaries=(), sessions=None, history=None):
        self.summaries = summaries
        self.sessions = sessions if sessions is not None else []
        self.history = history if history is not None else {}
        self.closed = False

    async def session_summaries(self):
        return self.summaries

    async def list_sessions(self):
        return self.sessions

    async def load_history(self, session_id):
        return self.history.get(session_id, [])

    def close(self):
        self.closed = True


class TestSessionHelpers:
    @pytest.mark.asyncio
    async def test_list_sessions_shapes_summaries(self):
        store = FakeStore(
            summaries=[
                {"session_id": "s1", "count": 3, "preview": "hello"},
                {"session_id": "s2", "count": 1, "preview": "world"},
            ]
        )
        assert await cli._list_sessions(store) == [("s1", 3, "hello"), ("s2", 1, "world")]

    @pytest.mark.asyncio
    async def test_pick_last_session(self):
        store = FakeStore(sessions=["s2", "s1"])
        assert await cli._pick_last_session(store) == "s2"

    @pytest.mark.asyncio
    async def test_pick_last_session_empty(self):
        store = FakeStore(sessions=[])
        assert await cli._pick_last_session(store) is None


class TestCmdList:
    @pytest.mark.asyncio
    async def test_empty_store(self, monkeypatch):
        st = make_state(monkeypatch)
        st.store = FakeStore(summaries=[])
        await cli._cmd_list(st, "")
        assert "no saved sessions" in _out(st)

    @pytest.mark.asyncio
    async def test_marks_current_session(self, monkeypatch):
        st = make_state(monkeypatch)
        st.session_id = "s1"
        st.store = FakeStore(
            summaries=[
                {"session_id": "s1", "count": 2, "preview": "current"},
                {"session_id": "s2", "count": 1, "preview": "other"},
            ]
        )
        await cli._cmd_list(st, "")
        out = _out(st)
        assert "s1" in out and "current" in out


class TestCmdResumeBranches:
    @pytest.mark.asyncio
    async def test_resume_explicit_missing_session(self, monkeypatch):
        st = make_state(monkeypatch)
        st.store = FakeStore(history={})
        await cli._cmd_resume(st, "nope")
        assert "Session not found" in _out(st)

    @pytest.mark.asyncio
    async def test_resume_no_sessions(self, monkeypatch):
        st = make_state(monkeypatch)
        st.store = FakeStore(sessions=[], history={})
        await cli._cmd_resume(st, "")
        assert "No sessions to resume" in _out(st)

    @pytest.mark.asyncio
    async def test_resume_found_sessions_empty(self, monkeypatch):
        st = make_state(monkeypatch)
        st.store = FakeStore(sessions=["s1"], history={})
        await cli._cmd_resume(st, "")
        assert "Session not found" in _out(st)


# ---------------------------------------------------------------------------
# /history truncation, /skill error branches
# ---------------------------------------------------------------------------


class TestCmdHistoryTruncation:
    @pytest.mark.asyncio
    async def test_long_content_gets_ellipsis(self, monkeypatch):
        st = make_state(monkeypatch)
        st.messages = [Message.user("x" * 120)]
        await cli._cmd_history(st, "")
        out = _out(st)
        assert "x" * 40 in out
        assert "..." in out

    @pytest.mark.asyncio
    async def test_content_with_newlines_collapsed(self, monkeypatch):
        st = make_state(monkeypatch)
        st.messages = [Message.user("line1\nline2")]
        await cli._cmd_history(st, "")
        assert "line1 line2" in _out(st)


class FakeSkill:
    def __init__(self, name, namespace="user", description="a skill"):
        self.name = name
        self.namespace = namespace
        self.description = description


class FakeSkillLoader:
    def __init__(self, skills=(), error=None):
        self.skills = skills
        self.error = error

    async def load(self):
        if self.error:
            raise self.error
        return self.skills


class TestCmdSkillBranches:
    @pytest.mark.asyncio
    async def test_list_empty_skills(self, monkeypatch):
        st = make_state(monkeypatch)
        st.agent.runner.skill_loader = FakeSkillLoader(skills=())
        await cli._cmd_skill(st, "list")
        assert "no skills found" in _out(st)

    @pytest.mark.asyncio
    async def test_list_load_error(self, monkeypatch):
        st = make_state(monkeypatch)
        st.agent.runner.skill_loader = FakeSkillLoader(error=RuntimeError("boom"))
        await cli._cmd_skill(st, "list")
        assert "failed to load skills" in _out(st)

    @pytest.mark.asyncio
    async def test_list_shows_disabled_and_missing_description(self, monkeypatch):
        st = make_state(monkeypatch)
        st.agent.runner.skill_loader = FakeSkillLoader(
            skills=(FakeSkill("alpha"), FakeSkill("beta", description=None))
        )
        st.disabled_skills = {"alpha"}
        await cli._cmd_skill(st, "list")
        out = _out(st)
        assert "alpha" in out and "disabled" in out
        assert "beta" in out


# ---------------------------------------------------------------------------
# ScriptRule outer exception path (script path that cannot be executed)
# ---------------------------------------------------------------------------


class TestScriptRuleEngineOuterError:
    async def test_script_without_python3_interpreter_denies(self, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_oserror)
        engine = PermissionEngine((ScriptRule("bash", {}, "whatever.py"),))
        decision = await engine.evaluate(ToolCall(id="c1", name="bash", arguments={}))
        assert decision.is_deny
        assert "external script error" in decision.reason


async def _raise_oserror(*a, **k):
    raise OSError("no python3")


# ---------------------------------------------------------------------------
# /resume with db_path reopen + /model with a closeable old llm
# ---------------------------------------------------------------------------


class TestResumeReopensStore:
    @pytest.mark.asyncio
    async def test_resume_with_db_path_reopens_store(self, monkeypatch, tmp_path):
        from microagent.core.store import InMemoryStore

        db = tmp_path / "sessions.db"
        st = make_state(monkeypatch)
        st.db_path = db
        st.store = FakeStore(
            sessions=["s1"],
            history={"s1": [Message.user("hello"), Message.assistant("hi")]},
        )

        async def fake_close():
            st.store.closed = True

        st.agent.close = fake_close  # type: ignore[attr-defined]
        reopen_calls = []
        live_store = InMemoryStore()

        def fake_reopen(path):
            reopen_calls.append(path)
            return live_store

        monkeypatch.setattr(cli, "_reopen_store", fake_reopen)
        await cli._cmd_resume(st, "s1")
        assert reopen_calls == [db]
        assert "Resumed: s1" in _out(st)


class TestCmdModelClosesOldLlm:
    @pytest.mark.asyncio
    async def test_switch_closes_old_closeable_llm(self, monkeypatch):
        from microagent.llm.client import LLMConfig

        closed = []

        class CloseableLLM:
            def __init__(self):
                self.config = LLMConfig("http://x/v1", "k", "old-model")

            async def close(self):
                closed.append(True)

        config = SimpleNamespace(llm=LLMConfig("http://x/v1", "k", "old-model"))
        runner = SimpleNamespace(memory=None, mode="build", llm=CloseableLLM())
        st = make_state(monkeypatch, runner=runner, config=config)
        await cli._cmd_model(st, "new-model")
        assert closed == [True]
        assert "Model switched" in _out(st)
        assert st.agent.runner.llm.config.model == "new-model"

    @pytest.mark.asyncio
    async def test_switch_without_close_does_not_raise(self, monkeypatch):
        from microagent.llm.client import LLMConfig

        config = SimpleNamespace(llm=LLMConfig("http://x/v1", "k", "old-model"))
        st = make_state(monkeypatch, config=config)
        await cli._cmd_model(st, "new-model")
        assert "Model switched" in _out(st)
        assert st.agent.runner.llm.config.model == "new-model"

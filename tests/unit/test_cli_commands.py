"""CLI surface tests — slash-command handlers and _UsageTracker.

Marks: cli. These drive the real _cmd_* handlers with a fake agent + a
StringIO-backed Console, verifying both output content and side effects.
"""

import io
import pytest

import microagent.surface.cli as cli
from microagent.core.store import InMemoryStore
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, Usage
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from tests.unit.fake_llm import FakeLLMClient, text_response


@pytest.fixture
def state():
    """A ReplState with a real runner (fake LLM) and captured console."""
    fake = FakeLLMClient([text_response("ok")])
    runner = SessionRunner(
        llm=fake,
        registry=ToolRegistry(_default_builtins()),
        budget=Budget.root(),
    )
    agent = cli.Agent(runner=runner, registry=runner.registry)
    config = type("C", (), {
        "llm": fake.config,
        "system_prompt": "test",
        "skills_path": None,
    })()
    st = cli.ReplState(
        agent=agent,
        config=config,
        store=InMemoryStore(),
        session_id="cli-test",
    )
    # Capture console output
    buf = io.StringIO()
    st._buf = buf
    st._orig_console = cli.console
    cli.console = cli.Console(file=buf, force_terminal=False, width=100)
    yield st
    cli.console = st._orig_console
    agent.close() if False else None  # closed in teardown via runner


def _captured(st):
    return st._buf.getvalue()


# --- _UsageTracker ---

class TestUsageTracker:
    def test_record_accumulates(self):
        t = cli._UsageTracker()
        t.record(Usage(input_tokens=100, output_tokens=50, cost_usd=0.01))
        t.record(Usage(input_tokens=200, output_tokens=100, cost_usd=0.02))
        assert t.total_input == 300
        assert t.total_output == 150
        assert t.total_cost == pytest.approx(0.03)
        assert t.turns == 2

    def test_reset(self):
        t = cli._UsageTracker()
        t.record(Usage(input_tokens=1, output_tokens=1, cost_usd=0.01))
        t.reset()
        assert t.total_input == 0 and t.total_output == 0
        assert t.total_cost == 0.0 and t.turns == 0

    def test_summary_contains_metrics(self, monkeypatch):
        monkeypatch.delenv("MICROAGENT_CURRENCY_RATE", raising=False)
        t = cli._UsageTracker()
        t.record(Usage(input_tokens=1000, output_tokens=500, cost_usd=0.05))
        s = t.summary()
        assert "1000" in s and "500" in s
        assert "¥" in s  # CNY display

    def test_status_line_format(self, monkeypatch):
        monkeypatch.delenv("MICROAGENT_CURRENCY_RATE", raising=False)
        t = cli._UsageTracker()
        t.record(Usage(input_tokens=10, output_tokens=5, cost_usd=0.01))
        line = t.status_line()
        assert "tokens:" in line
        assert "cost:" in line
        assert "turns:" in line


# --- Model command ---

@pytest.mark.asyncio
async def test_cmd_model_show_current(state):
    await cli._cmd_model(state, "")
    assert "Current model" in _captured(state)


@pytest.mark.asyncio
async def test_cmd_model_switch(state):
    from microagent.llm.client import OpenAIChatClient
    await cli._cmd_model(state, "gpt-4o-mini")
    assert "Model switched" in _captured(state)
    assert state.agent.runner.llm.config.model == "gpt-4o-mini"
    assert isinstance(state.agent.runner.llm, OpenAIChatClient)


# --- History command ---

@pytest.mark.asyncio
async def test_cmd_history_empty(state):
    await cli._cmd_history(state, "")
    assert "no messages" in _captured(state)


@pytest.mark.asyncio
async def test_cmd_history_with_messages(state):
    state.messages = [Message.user("hello world"), Message.assistant("hi there")]
    await cli._cmd_history(state, "")
    assert "hello world" in _captured(state)
    assert "user" in _captured(state)


# --- Cost command ---

@pytest.mark.asyncio
async def test_cmd_cost(state, monkeypatch):
    monkeypatch.delenv("MICROAGENT_CURRENCY_RATE", raising=False)
    state.usage_tracker.record(Usage(input_tokens=10, output_tokens=5, cost_usd=0.01))
    await cli._cmd_cost(state, "")
    out = _captured(state)
    assert "Tokens" in out


# --- Models command ---

@pytest.mark.asyncio
async def test_cmd_models_current(state, monkeypatch):
    monkeypatch.delenv("MICROAGENT_CURRENCY_RATE", raising=False)
    # config.llm.model is 'fake-model'
    await cli._cmd_models(state, "")
    out = _captured(state)
    assert "pricing" in out.lower()


@pytest.mark.asyncio
async def test_cmd_models_specific(state, monkeypatch):
    monkeypatch.delenv("MICROAGENT_CURRENCY_RATE", raising=False)
    await cli._cmd_models(state, "gpt-4o")
    out = _captured(state)
    assert "gpt-4o" in out
    assert "¥" in out  # CNY


@pytest.mark.asyncio
async def test_cmd_models_count(state):
    await cli._cmd_models(state, "count")
    assert "models in cache" in _captured(state)


@pytest.mark.asyncio
async def test_cmd_models_refresh(state, monkeypatch):
    """'/models refresh' previously crashed with NameError — asyncio was only
    imported inside main()/_run_streaming, invisible to _cmd_models."""
    from microagent.llm import pricing as _pricing

    monkeypatch.setattr(_pricing, "refresh", lambda: 364)
    await cli._cmd_models(state, "refresh")
    assert "364 models" in _captured(state)


# --- Skill command ---

@pytest.mark.asyncio
async def test_cmd_skill_no_loader(state):
    state.agent.runner.skill_loader = None
    await cli._cmd_skill(state, "list")
    assert "no skill loader" in _captured(state)


@pytest.mark.asyncio
async def test_cmd_skill_unload_requires_name(state):
    await cli._cmd_skill(state, "unload")
    assert "Usage: /skill unload" in _captured(state)


@pytest.mark.asyncio
async def test_cmd_skill_load_requires_name(state):
    await cli._cmd_skill(state, "load")
    assert "Usage: /skill load" in _captured(state)


@pytest.mark.asyncio
async def test_cmd_skill_unload_and_reload(state):
    await cli._cmd_skill(state, "unload my-skill")
    assert "my-skill" in state.disabled_skills
    await cli._cmd_skill(state, "load my-skill")
    assert "my-skill" not in state.disabled_skills
    # loading an already-enabled skill
    await cli._cmd_skill(state, "load other-skill")
    assert "already enabled" in _captured(state)


@pytest.mark.asyncio
async def test_cmd_skill_unknown_subcommand(state):
    await cli._cmd_skill(state, "bogus")
    assert "Unknown subcommand" in _captured(state)


@pytest.mark.asyncio
async def test_cmd_skill_list_with_loader(state):
    from microagent.skill.loader import ClaudeSkillLoader
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    sd = d / "s1"
    sd.mkdir()
    (sd / "SKILL.md").write_text("---\nname: s1\ndescription: test skill\n---\nbody\n")
    state.agent.runner.skill_loader = ClaudeSkillLoader(search_paths=(d,))
    await cli._cmd_skill(state, "list")
    out = _captured(state)
    assert "s1" in out


# --- Help / plan / build / thinking / clear ---

@pytest.mark.asyncio
async def test_cmd_help(state):
    await cli._cmd_help(state, "")
    out = _captured(state)
    assert "/models" in out
    assert "/cost" in out


@pytest.mark.asyncio
async def test_cmd_plan(state):
    await cli._cmd_plan(state, "")
    assert state.agent.runner.mode == "plan"


@pytest.mark.asyncio
async def test_cmd_build(state):
    state.agent.runner.mode = "plan"
    await cli._cmd_build(state, "")
    assert state.agent.runner.mode == "build"


# --- _make_agent ---

def test_make_agent_returns_agent(state):
    agent = cli._make_agent(state.config, state.store, "new-session")
    assert isinstance(agent, cli.Agent)
    # CLI wires the permission engine with an interactive ASK callback —
    # permission.py's 'CLI/Web injects one' contract
    assert agent.runner.permission_engine is not None
    assert agent.runner.permission_engine.ask_callback is not None
    assert agent is not None
    assert agent.runner.session_id == "new-session"


@pytest.mark.asyncio
async def test_cli_ask_callback_allows_on_yes(monkeypatch):
    """The CLI ask callback prompts and maps y/n to ALLOW/DENY."""
    engine = cli._make_permission_engine()
    from microagent.core.permission import Decision
    from microagent.core.types import ToolCall

    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "y")
    call = ToolCall(id="c1", name="task", arguments={"prompt": "x"})
    decision = await engine.evaluate(call)
    assert decision.decision is Decision.ALLOW

    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "n")
    decision = await engine.evaluate(call)
    assert decision.decision is Decision.DENY


class TestCLIHelpers:
    def test_short_args_simple(self):
        assert cli._short_args({"a": "1", "b": "hello world"}) == '1 "hello world"'

    def test_short_args_long_value(self):
        long = "x" * 100
        s = cli._short_args({"a": long})
        assert len(s) <= 61  # truncated to 60
        assert s.endswith("...")

    def test_short_args_empty(self):
        assert cli._short_args({}) == ""

    def test_short_args_non_string_values(self):
        assert cli._short_args({"n": 42, "b": True}) == "42 True"

    def test_summarize_cleans_newlines(self):
        assert cli._summarize("line1\nline2") == "line1 line2"

    def test_summarize_truncates_long(self):
        long = "x" * 100
        s = cli._summarize(long)
        assert len(s) <= 70
        assert s.endswith("...")

    def test_summarize_short(self):
        assert cli._summarize("short") == "short"

    def test_render_content_plain(self):
        """Plain content renders as Markdown without error."""
        buf = io.StringIO()
        orig = cli.console
        cli.console = cli.Console(file=buf, force_terminal=False, width=80)
        try:
            cli._render_content("hello **world**")
        finally:
            cli.console = orig
        assert "hello" in buf.getvalue()

    def test_render_content_with_code(self):
        buf = io.StringIO()
        orig = cli.console
        cli.console = cli.Console(file=buf, force_terminal=False, width=80)
        try:
            cli._render_content("text before\n```python\nprint('hi')\n```\ntext after")
        finally:
            cli.console = orig
        out = buf.getvalue()
        assert "text before" in out
        assert "text after" in out
        assert "print" in out  # code content rendered

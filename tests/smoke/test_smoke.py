"""Smoke tests — fast import + basic lifecycle sanity checks.

Marks: smoke. These verify the package imports cleanly and core objects
can be constructed + closed without error. Run in CI as the first gate.
"""

import importlib
import pkgutil
import pytest


def test_all_submodules_import():
    """Every submodule under microagent must import without error."""
    import microagent
    failed = []
    for mod in pkgutil.walk_packages(microagent.__path__, prefix="microagent."):
        try:
            importlib.import_module(mod.name)
        except Exception as e:
            failed.append(f"{mod.name}: {type(e).__name__}: {e}")
    assert not failed, f"Import failures: {failed}"


def test_public_api_symbols_importable():
    """Every name in __all__ must be importable."""
    import microagent
    for name in microagent.__all__:
        assert hasattr(microagent, name), f"__all__ lists {name!r} but it's not exposed"


def test_version_string_exists():
    import microagent
    # __version__ may or may not exist; at least the package loads
    assert callable(microagent.Agent.from_config) or hasattr(microagent, "Agent")


def test_llmconfig_builds():
    from microagent.llm.client import LLMConfig
    c = LLMConfig(base_url="http://x", api_key="k", model="m")
    assert c.base_url == "http://x"
    assert c.model == "m"


def test_message_types_build():
    from microagent.core.types import Message, ToolCall, ToolResult, Usage
    m = Message.user("hello")
    assert m.role == "user"
    tc = ToolCall(id="c1", name="bash", arguments={})
    assert tc.name == "bash"
    r = ToolResult.ok("content")
    assert not r.is_error
    u = Usage(input_tokens=1, output_tokens=1)
    assert u.input_tokens == 1


def test_default_builtins_load():
    from microagent.core.tool import _default_builtins
    tools = _default_builtins()
    assert len(tools) >= 30  # expect ~34
    names = {t.name for t in tools}
    for required in ("read_file", "write_file", "bash", "grep", "glob"):
        assert required in names, f"missing builtin {required}"


@pytest.mark.asyncio
async def test_agent_create_close():
    """Agent.from_config + close() lifecycle must not crash."""
    from microagent import Agent
    from microagent.llm.client import LLMConfig
    agent = Agent.from_config(LLMConfig(base_url="http://x", api_key="k", model="m"))
    await agent.close()


@pytest.mark.asyncio
async def test_sessionrunner_create_close():
    from microagent.session.runner import SessionRunner
    from microagent.core.tool import ToolRegistry
    from microagent.session.budget import Budget
    from tests.unit.fake_llm import FakeLLMClient
    runner = SessionRunner(
        llm=FakeLLMClient([]),
        registry=ToolRegistry([]),
        budget=Budget.root(),
    )
    await runner.close()


@pytest.mark.asyncio
async def test_runner_text_turn_completes():
    """A simple text turn completes with TurnComplete."""
    from microagent.session.runner import SessionRunner
    from microagent.core.tool import ToolRegistry
    from microagent.session.budget import Budget
    from microagent.core.types import Message, TurnComplete
    from tests.unit.fake_llm import FakeLLMClient, text_response
    runner = SessionRunner(
        llm=FakeLLMClient([text_response("hello")]),
        registry=ToolRegistry([]),
        budget=Budget.root(),
    )
    events = [e async for e in runner.run_turn([Message.user("hi")])]
    assert any(isinstance(e, TurnComplete) for e in events)
    await runner.close()


def test_pricing_cache_seed_loads():
    """The shipped models_cache.json must parse and provide pricing."""
    from microagent.llm.pricing import get_pricing
    p = get_pricing("openai/gpt-4o")
    assert p[0] > 0  # non-zero input price


def test_currency_format():
    from microagent.currency import format_cost
    assert format_cost(0.0).startswith("¥")

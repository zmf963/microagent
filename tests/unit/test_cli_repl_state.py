"""Regression test: CLI /new, /resume, /compact must rebind ReplState.

Before the fix, the REPL loop in _main() read stale local variables
(agent, messages) instead of repl_state. After /new closed the old agent
and built a fresh one on state.agent, the loop still drove the closed
agent — /new, /resume, and /compact were effectively no-ops (or worse,
used-after-close).

These tests verify that the command handlers rebind state.agent /
state.messages, and that the loop contract (read repl_state, not locals)
is structurally present in _main's source.
"""

import inspect
import pytest

from microagent.surface.cli import ReplState, _main, _cmd_new
from microagent.core.types import Message


def test_main_loop_reads_repl_state_not_locals():
    """Structural assertion: the REPL loop body uses repl_state.agent/messages.

    Guards against regression where someone reintroduces `messages.append(...)`
    or `_run_streaming(agent, ...)` with bare locals.
    """
    src = inspect.getsource(_main)
    assert "repl_state.messages.append(Message.user" in src, (
        "_main must append to repl_state.messages (not bare local `messages`) "
        "so /new, /resume, /compact take effect"
    )
    assert "_run_streaming(\n            repl_state.agent," in src or \
           "_run_streaming(repl_state.agent" in src or \
           "repl_state.agent," in src, (
        "_main must stream from repl_state.agent (not bare local `agent`)"
    )
    # And the closing cleanup must use repl_state.agent too.
    assert "await repl_state.agent.close()" in src, (
        "_main cleanup must close repl_state.agent (the live one after any /new)"
    )


@pytest.mark.asyncio
async def test_cmd_new_rebinds_agent_and_messages():
    """/new should close the old agent and put a fresh agent + empty messages
    onto state, so the next loop iteration uses the new session."""
    from microagent.surface import cli
    from microagent.agent import Agent
    from microagent.llm.client import LLMConfig
    from microagent.core.store import InMemoryStore
    from microagent.core.tool import ToolRegistry, _default_builtins
    from microagent.session.runner import SessionRunner
    from microagent.session.budget import Budget
    from tests.unit.fake_llm import FakeLLMClient, text_response

    fake = FakeLLMClient([text_response("ok")])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
    )
    agent = Agent(runner=runner, registry=runner.registry)

    config = type("C", (), {
        "llm": fake.config,
        "system_prompt": "test",
        "skills_path": None,
    })()
    state = ReplState(
        agent=agent,
        config=config,
        store=InMemoryStore(),
        session_id="old-session",
        messages=[Message.user("hi"), Message.assistant("hello")],
    )
    old_agent = state.agent

    await cli._cmd_new(state, "")

    # Agent was replaced with a new instance
    assert state.agent is not old_agent, "/new did not rebind state.agent"
    # Messages were cleared
    assert state.messages == [], "/new did not clear state.messages"
    # Session id changed
    assert state.session_id != "old-session"
    await state.agent.close()
    await old_agent.runner.close()


@pytest.mark.asyncio
async def test_cmd_resume_rebinds_messages():
    """/resume should load history into state.messages so the loop sees it."""
    from microagent.surface import cli
    from microagent.agent import Agent
    from microagent.core.store import InMemoryStore
    from microagent.core.tool import ToolRegistry, _default_builtins
    from microagent.session.runner import SessionRunner
    from microagent.session.budget import Budget
    from tests.unit.fake_llm import FakeLLMClient, text_response

    fake = FakeLLMClient([text_response("ok")])
    runner = SessionRunner(
        llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
    )
    store = InMemoryStore()
    # Seed history
    await store.append("sess-X", Message.user("seed user"))
    await store.append("sess-X", Message.assistant("seed assistant"))

    agent = Agent(runner=runner, registry=runner.registry)
    config = type("C", (), {
        "llm": fake.config, "system_prompt": "test", "skills_path": None,
    })()
    state = ReplState(
        agent=agent,
        config=config,
        store=store,
        session_id="other",
        messages=[],
    )

    await cli._cmd_resume(state, "sess-X")

    assert state.session_id == "sess-X"
    assert len(state.messages) == 2, f"expected 2 loaded msgs, got {len(state.messages)}"
    assert state.messages[0].content == "seed user"
    await state.agent.close()

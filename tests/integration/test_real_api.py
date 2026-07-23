"""Integration tests against a real OpenAI-compatible API.

Set env vars to enable:
    MICROAGENT_TEST_BASE_URL=http://10.144.0.2:20128/v1
    MICROAGENT_TEST_API_KEY=sk-...
    MICROAGENT_TEST_MODEL=oc-d4f

If any of these are missing, tests are skipped automatically.
"""

import os
import tempfile

import pytest

from microagent import Agent, Config, Message, LLMConfig, SQLiteStore

SKIP = not all(os.environ.get(k) for k in (
    "MICROAGENT_TEST_BASE_URL",
    "MICROAGENT_TEST_API_KEY",
    "MICROAGENT_TEST_MODEL",
))

pytestmark = pytest.mark.skipif(
    SKIP,
    reason="Set MICROAGENT_TEST_* env vars to run integration tests",
)


def _get_config() -> LLMConfig:
    return LLMConfig(
        base_url=os.environ["MICROAGENT_TEST_BASE_URL"],
        api_key=os.environ["MICROAGENT_TEST_API_KEY"],
        model=os.environ["MICROAGENT_TEST_MODEL"],
    )


# =========================================================================
# Basic integration tests
# =========================================================================

@pytest.mark.integration
async def test_simple_chat():
    """A simple text exchange without tool calls."""
    agent = Agent.from_config(_get_config(), max_iterations=3)
    messages = [Message.user("Say exactly 'pong' and nothing else.")]
    result = await agent.arun(messages)
    assert "pong" in result.lower()


@pytest.mark.integration
async def test_tool_call_read_file(tmp_path):
    """LLM calls read_file tool to read a file."""
    test_file = tmp_path / "hello.txt"
    test_file.write_text("hello from integration test\n")

    agent = Agent.from_config(
        _get_config(),
        system_prompt=(
            "You are a test assistant. When asked to read a file, "
            "use the read_file tool. Report exactly what you read."
        ),
        max_iterations=5,
    )
    messages = [Message.user(f"Read the file {test_file} and tell me its contents.")]
    result = await agent.arun(messages)
    assert "hello from integration test" in result


@pytest.mark.integration
async def test_tool_call_bash():
    """LLM calls bash tool to execute a command."""
    agent = Agent.from_config(
        _get_config(),
        system_prompt=(
            "You are a test assistant. When asked to run a command, "
            "use the bash tool. Report the exact output."
        ),
        max_iterations=5,
    )
    messages = [Message.user("Run the command 'echo integration_test_ok' and report the output.")]
    result = await agent.arun(messages)
    assert "integration_test_ok" in result


@pytest.mark.integration
async def test_multi_turn_conversation():
    """Multi-turn conversation with memory of previous turn."""
    agent = Agent.from_config(_get_config(), max_iterations=5)
    messages: list[Message] = [
        Message.user("Remember: my secret code is BANANA42. Just acknowledge.")
    ]
    await agent.arun(messages)
    messages.append(Message.user("What is my secret code?"))
    result = await agent.arun(messages)
    assert "BANANA42" in result


# =========================================================================
# Deep integration tests: multi-tool, compression, session resume
# =========================================================================

@pytest.mark.integration
async def test_multi_tool_task(tmp_path):
    """Agent completes a multi-step task using bash + write_file + read_file."""
    test_dir = tmp_path / "project"
    test_dir.mkdir()

    agent = Agent.from_config(
        _get_config(),
        system_prompt=(
            "You are a coding assistant. Follow instructions precisely. "
            f"Work in directory: {test_dir}"
        ),
        max_iterations=10,
    )
    messages = [
        Message.user(
            f"Create a Python script at {test_dir}/hello.py that prints 'Hello, World!'. "
            f"Then run it with python3 and confirm the output."
        )
    ]
    result = await agent.arun(messages)

    # Verify file was created
    hello_py = test_dir / "hello.py"
    if not hello_py.exists():
        # Agent might have used a different path
        import glob
        candidates = list(test_dir.rglob("hello.py"))
        if candidates:
            hello_py = candidates[0]

    assert hello_py.exists(), f"hello.py not found in {list(test_dir.iterdir())}"
    content = hello_py.read_text()
    assert "Hello" in content or "print" in content.lower()


@pytest.mark.integration
async def test_session_persistence_and_resume(tmp_path):
    """Session persists to SQLiteStore and can be resumed."""
    db_path = tmp_path / "test_sessions.db"
    store = SQLiteStore(db_path)

    # Turn 1: create a persistent session
    agent1 = Agent.from_config(
        _get_config(),
        system_prompt="You are a test assistant. Your knowledge code is XYZZY99.",
        store=store,
        session_id="test-session-persist",
        max_iterations=5,
    )
    messages1 = [Message.user("Say your knowledge code and nothing else.")]
    result1 = await agent1.arun(messages1)
    assert "XYZZY99" in result1

    # Verify persisted
    history = await store.load_history("test-session-persist")
    assert len(history) >= 2  # user + assistant

    # Turn 2: resume and ask what was said
    agent2 = Agent.from_config(
        _get_config(),
        store=store,
        session_id="test-session-persist",
        max_iterations=5,
    )
    messages2 = list(history) + [Message.user("What code did you give me in the first message?")]
    result2 = await agent2.arun(messages2)
    assert "XYZZY99" in result2

    store.close()


@pytest.mark.integration
async def test_compaction_with_tool_calls(tmp_path):
    """Manually compact a multi-tool conversation and verify summary quality."""
    agent = Agent.from_config(
        _get_config(),
        system_prompt="You are a coding assistant. Use tools when asked.",
        max_iterations=5,
    )

    # Build a conversation with tool calls
    messages: list[Message] = [
        Message.user("Run 'echo compact_test_step1'."),
    ]
    await agent.arun(messages)

    messages.append(Message.user("Run 'echo compact_test_step2'."))
    await agent.arun(messages)

    messages.append(Message.user("Run 'echo compact_test_step3'."))
    await agent.arun(messages)

    # Now compact
    from microagent.session.compress import compact_conversation, count_tokens

    before_tokens = count_tokens(tuple(messages))
    compressed = await compact_conversation(
        tuple(messages), agent.runner.llm,
        context_window=before_tokens + 8000,
        force=True,
    )

    after_tokens = count_tokens(compressed)
    # Compaction restructures the conversation (fewer messages with summary)
    assert len(compressed) < len(messages), (
        f"Compaction should reduce message count: "
        f"{len(messages)} → {len(compressed)}"
    )

    # Compressed result should mention the steps
    summary_text = " ".join(m.content for m in compressed if m.role == "user")
    assert "compact_test_step" in summary_text or "echo" in summary_text.lower()

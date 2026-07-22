"""Integration tests against a real OpenAI-compatible API.

Set env vars to enable:
    MICROAGENT_TEST_BASE_URL=http://10.144.0.2:20128/v1
    MICROAGENT_TEST_API_KEY=sk-...
    MICROAGENT_TEST_MODEL=oc-d4f

If any of these are missing, tests are skipped automatically.
"""

import os
import pytest
from microagent import Agent, Message, LLMConfig

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


@pytest.mark.integration
async def test_simple_chat():
    """Test: a simple text exchange without tool calls."""
    agent = Agent.from_config(_get_config(), max_iterations=3)
    messages = [Message.user("Say exactly 'pong' and nothing else.")]
    result = await agent.arun(messages)
    assert "pong" in result.lower()


@pytest.mark.integration
async def test_tool_call_read_file(tmp_path):
    """Test: LLM calls read_file tool to read a file."""
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
    """Test: LLM calls bash tool to execute a command."""
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
    """Test: a multi-turn conversation with memory of previous turn."""
    agent = Agent.from_config(_get_config(), max_iterations=5)
    messages: list[Message] = [
        Message.user("Remember: my secret code is BANANA42. Just acknowledge.")
    ]
    await agent.arun(messages)
    messages.append(Message.user("What is my secret code?"))
    result = await agent.arun(messages)
    assert "BANANA42" in result

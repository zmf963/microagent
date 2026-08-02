"""Performance benchmarks for MicroAgent core paths.

Run: pytest tests/benchmark/ -v -s
Benchmark tests are marked @pytest.mark.benchmark and skipped by default.
Use --benchmark flag or run directly to execute.
"""

import time

import pytest

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import Message, ToolCall, ToolResult
from microagent.session.compress import (
    count_tokens,
    micro_compact,
    snip_tool_results,
)
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner


pytestmark = pytest.mark.benchmark


class TestToolExecutionLatency:
    """Benchmark: tool execution framework overhead (mock, no real I/O)."""

    async def test_bash_tool_execution_time(self):
        """bash tool execution should be <100ms for simple commands."""
        reg = ToolRegistry(_default_builtins())
        call = ToolCall(id="bench1", name="bash", arguments={"command": "echo benchmark"})

        start = time.perf_counter()
        result = await reg.execute(call)
        elapsed = time.perf_counter() - start

        assert not result.is_error
        assert elapsed < 0.1, f"bash execution took {elapsed:.3f}s (expected <0.1s)"

    async def test_read_file_execution_time(self, tmp_path):
        """read_file should be <50ms for small files."""
        test_file = tmp_path / "bench.txt"
        test_file.write_text("benchmark line\n" * 100)

        reg = ToolRegistry(_default_builtins())
        call = ToolCall(id="bench2", name="read_file", arguments={"path": str(test_file)})

        start = time.perf_counter()
        result = await reg.execute(call)
        elapsed = time.perf_counter() - start

        assert not result.is_error
        assert elapsed < 0.05, f"read_file took {elapsed:.3f}s (expected <0.05s)"


class TestCompressionPerformance:
    """Benchmark: compression layer performance on synthetic data."""

    def test_micro_compact_100_messages(self):
        """L1 micro_compact on 100 messages should be <50ms."""
        tc = ToolCall(id="tc", name="bash", arguments={})
        messages = []
        for i in range(50):
            messages.append(Message.assistant(f"resp {i}", tool_calls=(tc,)))
            messages.append(Message.tool_result(
                ToolResult.ok("x" * 1000), tool_call_id="tc"
            ))
        messages = tuple(messages)

        start = time.perf_counter()
        result = micro_compact(messages)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.05, f"micro_compact took {elapsed:.3f}s (expected <0.05s)"
        # Verify it actually compressed
        assert count_tokens(result) < count_tokens(messages)

    def test_snip_tool_results_100_messages(self):
        """L2 snip on 100 messages should be <50ms."""
        tc = ToolCall(id="tc", name="bash", arguments={})
        messages = []
        for i in range(50):
            messages.append(Message.assistant(f"resp {i}", tool_calls=(tc,)))
            messages.append(Message.tool_result(
                ToolResult.ok("x" * 1000), tool_call_id="tc"
            ))
        messages = tuple(messages)

        start = time.perf_counter()
        result = snip_tool_results(messages, keep_recent=10, max_tokens=5000)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.05, f"snip took {elapsed:.3f}s (expected <0.05s)"


class TestSchemaInference:
    """Benchmark: Pydantic v2 schema inference for all tools."""

    def test_schema_generation_all_tools(self):
        """to_openai_tools() for 24 tools should be <100ms."""
        reg = ToolRegistry(_default_builtins())

        start = time.perf_counter()
        schemas = reg.to_openai_tools()
        elapsed = time.perf_counter() - start

        assert schemas is not None
        assert len(schemas) >= 20  # at least 20 tools
        assert elapsed < 0.1, f"schema gen took {elapsed:.3f}s (expected <0.1s)"

    def test_schema_generation_cached(self):
        """Second call to to_openai_tools() should be near-instant (if cached)."""
        reg = ToolRegistry(_default_builtins())

        # First call
        reg.to_openai_tools()

        start = time.perf_counter()
        schemas = reg.to_openai_tools()
        elapsed = time.perf_counter() - start

        assert schemas is not None
        assert elapsed < 0.01, f"cached schema gen took {elapsed:.3f}s (expected <0.01s)"


class TestLLMRealAPI:
    """Benchmark: real LLM API end-to-end latency (P50/P95).

    Skipped unless MICROAGENT_TEST_* env vars are set.
    """

    @pytest.mark.integration
    async def test_single_turn_latency(self):
        """Single turn (user → tool call → result → text) end-to-end latency."""
        import os

        base_url = os.environ.get("MICROAGENT_TEST_BASE_URL")
        api_key = os.environ.get("MICROAGENT_TEST_API_KEY")
        model = os.environ.get("MICROAGENT_TEST_MODEL")

        if not all([base_url, api_key, model]):
            pytest.skip("MICROAGENT_TEST_* env vars not set")

        from microagent.llm.client import LLMConfig, OpenAIChatClient

        config = LLMConfig(base_url=base_url, api_key=api_key, model=model)
        llm = OpenAIChatClient(config)
        reg = ToolRegistry(_default_builtins())
        runner = SessionRunner(
            llm=llm,
            registry=reg,
            budget=Budget.root(max_iterations=5),
        )

        messages = [Message.user("Say 'hello world' and nothing else.")]

        start = time.perf_counter()
        async for event in runner.run_turn(messages):
            pass
        elapsed = time.perf_counter() - start

        await llm.close()

        # Single turn should complete in <30s for a simple prompt
        assert elapsed < 30.0, f"single turn took {elapsed:.1f}s (expected <30s)"
        print(f"\n  Single turn latency: {elapsed:.2f}s")

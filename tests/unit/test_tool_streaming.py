"""Tests for core/tool.py uncovered paths: streaming, schema edge cases,
ToolProgressDelta, and error handling in execute_stream."""

import pytest
from typing import Annotated
from pydantic import Field

from microagent.core.tool import (
    FunctionTool,
    ToolRegistry,
    ToolProgressDelta,
    tool,
)
from microagent.core.types import ToolCall, ToolResult


class TestExecuteStream:
    @pytest.mark.asyncio
    async def test_async_generator_streams(self):
        """An async-generator tool streams ToolProgressDelta then a result."""
        async def gen():
            yield "chunk1"
            yield "chunk2"

        ft = FunctionTool(name="gen", fn=gen, parameters={}, description="gen")
        events = [e async for e in ft.execute_stream(
            ToolCall(id="c1", name="gen", arguments={}))]
        # 2 progress + 1 final result
        progress = [e for e in events if isinstance(e, ToolProgressDelta)]
        result = [e for e in events if isinstance(e, ToolResult)]
        assert len(progress) == 2
        assert "chunk1" in progress[0].text
        assert "chunk2" in progress[1].text
        assert len(result) == 1
        assert result[0].content == "chunk1chunk2"

    @pytest.mark.asyncio
    async def test_stream_error_returns_error(self):
        async def gen():
            yield "partial"
            raise RuntimeError("boom mid-stream")

        ft = FunctionTool(name="gen", fn=gen, parameters={}, description="gen")
        events = [e async for e in ft.execute_stream(
            ToolCall(id="c1", name="gen", arguments={}))]
        # progress event + error result
        result = [e for e in events if isinstance(e, ToolResult)]
        assert len(result) == 1
        assert result[0].is_error
        assert "failed" in result[0].content

    @pytest.mark.asyncio
    async def test_non_streaming_toolresult(self):
        async def fn():
            return ToolResult.ok("direct result")

        ft = FunctionTool(name="f", fn=fn, parameters={}, description="f")
        events = [e async for e in ft.execute_stream(ToolCall(id="c1", name="f", arguments={}))]
        assert len(events) == 1
        assert isinstance(events[0], ToolResult)
        assert events[0].content == "direct result"

    @pytest.mark.asyncio
    async def test_non_streaming_plain_value(self):
        """A non-ToolResult return value is wrapped in ToolResult.ok."""
        async def fn():
            return "plain string"

        ft = FunctionTool(name="f", fn=fn, parameters={}, description="f")
        events = [e async for e in ft.execute_stream(ToolCall(id="c1", name="f", arguments={}))]
        assert len(events) == 1
        assert isinstance(events[0], ToolResult)
        assert events[0].content == "plain string"

    @pytest.mark.asyncio
    async def test_async_gen_progress_delta_passthrough(self):
        async def gen():
            yield ToolProgressDelta(id="c1", name="g", text="progress!")
            yield "tail"

        ft = FunctionTool(name="g", fn=gen, parameters={}, description="g")
        events = [e async for e in ft.execute_stream(ToolCall(id="c1", name="g", arguments={}))]
        progress = [e for e in events if isinstance(e, ToolProgressDelta)]
        result = [e for e in events if isinstance(e, ToolResult)]
        # explicit delta + string "tail" both become progress deltas
        assert len(progress) == 2
        assert progress[0].text == "progress!"
        assert progress[1].text == "tail"
        assert len(result) == 1


class TestSchemaInference:
    @pytest.mark.asyncio
    async def test_optional_param_not_required(self):
        @tool("test_optional_probe")
        async def fn(a: Annotated[str, Field(description="a")],
                     b: Annotated[int, Field(description="b")] = 5) -> ToolResult:
            return ToolResult.ok("")

        schema = fn.parameters
        assert "a" in schema["properties"]
        assert "b" in schema["properties"]
        # a is required, b is not (has default)
        assert schema["required"] == ["a"]

    @pytest.mark.asyncio
    async def test_self_and_ctx_skipped(self):
        class C:
            @tool("test_method_probe")
            async def method(self, ctx=None, x: int = 0) -> ToolResult:
                return ToolResult.ok("")

        ft = C.method
        schema = ft.parameters
        # self and ctx are skipped
        assert "self" not in schema["properties"]
        assert "ctx" not in schema["properties"]
        assert "x" in schema["properties"]

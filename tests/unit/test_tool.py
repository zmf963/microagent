"""Tests for @tool decorator, schema inference, and ToolRegistry."""

from typing import Annotated

from pydantic import Field

from microagent.core.tool import ToolRegistry, tool
from microagent.core.types import ToolCall, ToolResult


class TestToolDecorator:
    def test_basic_registration(self):
        @tool("test_tool_1", description="A test tool")
        async def test_tool_1(
            path: Annotated[str, Field(description="The file path")],
            count: Annotated[int, Field(description="Number", ge=1)] = 10,
        ) -> ToolResult:
            return ToolResult.ok(f"{path}:{count}")

        assert test_tool_1.name == "test_tool_1"
        assert test_tool_1.description == "A test tool"

    def test_schema_inference(self):
        @tool("test_schema", description="schema test")
        async def test_schema(
            name: Annotated[str, Field(description="User name")],
            age: Annotated[int, Field(description="Age", ge=0, le=150)] = 25,
        ) -> ToolResult:
            return ToolResult.ok("ok")

        params = test_schema.parameters
        assert params["type"] == "object"
        assert "name" in params["properties"]
        assert "age" in params["properties"]
        assert "name" in params["required"]
        assert "age" not in params["required"]

    def test_auto_description_from_docstring(self):
        @tool("test_doc")
        async def test_doc(x: Annotated[str, Field()]) -> ToolResult:
            """This is the description."""
            return ToolResult.ok("ok")

        assert test_doc.description == "This is the description."


class TestToolRegistry:
    def test_register_and_get(self):
        @tool("reg_test_1", description="test")
        async def reg_test_1(x: Annotated[str, Field()]) -> ToolResult:
            return ToolResult.ok(f"got {x}")

        registry = ToolRegistry([reg_test_1])
        assert registry.get("reg_test_1") is not None
        assert registry.get("nonexistent") is None

    def test_duplicate_raises(self):
        @tool("dup_test", description="test")
        async def dup_test(x: Annotated[str, Field()]) -> ToolResult:
            return ToolResult.ok("ok")

        registry = ToolRegistry([dup_test])
        try:
            registry.register(dup_test)
            assert False, "should have raised"
        except ValueError:
            pass

    def test_to_openai_tools(self):
        @tool("oai_test", description="for openai export")
        async def oai_test(x: Annotated[str, Field(description="param")]) -> ToolResult:
            return ToolResult.ok("ok")

        registry = ToolRegistry([oai_test])
        tools = registry.to_openai_tools()
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "oai_test"

    async def test_execute(self):
        @tool("exec_test", description="exec")
        async def exec_test(msg: Annotated[str, Field(description="message")]) -> ToolResult:
            return ToolResult.ok(f"echo: {msg}")

        registry = ToolRegistry([exec_test])
        call = ToolCall(id="c1", name="exec_test", arguments={"msg": "hello"})
        result = await registry.execute(call)
        assert result.content == "echo: hello"
        assert not result.is_error

    async def test_execute_unknown(self):
        registry = ToolRegistry()
        call = ToolCall(id="c1", name="nonexistent", arguments={})
        result = await registry.execute(call)
        assert result.is_error
        assert "unknown tool" in result.content


class TestBadArguments:
    """When the LLM emits malformed tool-call argument JSON, client.py
    falls back to {"_raw": ...}; calling fn(**{"_raw": ...}) raises
    TypeError for any tool whose signature lacks a _raw parameter.
    That must become a ToolResult.error, not an exception."""

    async def test_raw_arguments_become_error_result(self):
        @tool("needs_path", description="needs a path")
        async def needs_path(path: Annotated[str, Field(description="p")]) -> ToolResult:
            return ToolResult.ok(path)

        registry = ToolRegistry([needs_path])
        call = ToolCall(id="c1", name="needs_path", arguments={"_raw": '{"path": "a.txt'})
        result = await registry.execute(call)
        assert result.is_error
        assert "_raw" in result.content  # raw snippet surfaced for diagnosis

    async def test_missing_required_arg_becomes_error_result(self):
        @tool("needs_path2", description="needs a path")
        async def needs_path2(path: Annotated[str, Field(description="p")]) -> ToolResult:
            return ToolResult.ok(path)

        registry = ToolRegistry([needs_path2])
        call = ToolCall(id="c1", name="needs_path2", arguments={})
        result = await registry.execute(call)
        assert result.is_error
        assert "needs_path2" in result.content

    async def test_raw_arguments_streaming_becomes_error_result(self):
        @tool("needs_path3", description="needs a path")
        async def needs_path3(path: Annotated[str, Field(description="p")]) -> ToolResult:
            return ToolResult.ok(path)

        registry = ToolRegistry([needs_path3])
        call = ToolCall(id="c1", name="needs_path3", arguments={"_raw": "garbage"})
        events = [e async for e in registry.execute_stream(call)]
        results = [e for e in events if isinstance(e, ToolResult)]
        assert len(results) == 1
        assert results[0].is_error
        assert "_raw" in results[0].content

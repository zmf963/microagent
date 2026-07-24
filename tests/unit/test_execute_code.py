"""Tests for execute_code builtin tool."""

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


class TestExecuteCode:
    async def test_execute_simple(self):
        """Execute a simple Python expression."""
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(
            id="c1",
            name="execute_code",
            arguments={
                "code": "print('hello from execute_code')",
            },
        )
        result = await registry.execute(call)
        assert not result.is_error
        assert "hello from execute_code" in result.content

    async def test_execute_with_imports(self):
        """Execute code with stdlib imports."""
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(
            id="c1",
            name="execute_code",
            arguments={
                "code": "import json; print(json.dumps({'key': 'value'}))",
            },
        )
        result = await registry.execute(call)
        assert not result.is_error
        assert '{"key": "value"}' in result.content

    async def test_execute_error(self):
        """Code with error returns error result."""
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(
            id="c1",
            name="execute_code",
            arguments={
                "code": "raise ValueError('test error')",
            },
        )
        result = await registry.execute(call)
        assert result.is_error

    async def test_execute_timeout(self):
        """Code that runs too long is killed."""
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(
            id="c1",
            name="execute_code",
            arguments={
                "code": "import time; time.sleep(10)",
                "timeout": 0.5,
            },
        )
        result = await registry.execute(call)
        assert result.is_error
        assert "timed out" in result.content.lower()

    async def test_registered_as_builtin(self):
        registry = ToolRegistry(_default_builtins())
        assert "execute_code" in registry.names

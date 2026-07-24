"""Tool Protocol, ToolRegistry, and @tool decorator.

Tools are the agent's way of interacting with the world. Every tool
implements the ``Tool`` Protocol (PEP 544 structural subtyping) — no
base class inheritance required.

Schema inference for ``@tool`` decorated functions uses Pydantic v2:
parameter type annotations (via ``Annotated[T, Field(...)]``) are
compiled into an OpenAI-compatible JSON Schema.

Streaming tools: functions that return ``AsyncIterator[str]`` get
automatic streaming support — each yielded string is emitted as a
``ToolProgressDelta``, giving users real-time progress.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol, get_type_hints, runtime_checkable

from pydantic import create_model
from pydantic.fields import FieldInfo

from .types import ToolCall, ToolProgressDelta, ToolResult

# ---------------------------------------------------------------------------
# TurnContext forward reference
# ---------------------------------------------------------------------------

TurnContext = Any  # forward reference placeholder


# ---------------------------------------------------------------------------
# Tool Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Tool(Protocol):
    """The narrow waist — every tool implements this interface."""

    name: str
    description: str
    parameters: dict[str, Any]  # OpenAI function JSON Schema

    async def execute(self, call: ToolCall, ctx: TurnContext | None = None) -> ToolResult: ...


# Sentinel to signal streaming completion
_STREAM_END = object()


# ---------------------------------------------------------------------------
# FunctionTool — wraps a plain async function into a Tool
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FunctionTool:
    """Adapter that wraps an ``async def`` function into a Tool.

    Supports streaming: if the wrapped function returns an
    ``AsyncIterator[str]`` (i.e. is an async generator), the
    tool yields ``ToolProgressDelta`` events for each chunk and
    returns a final ``ToolResult``.
    """

    name: str
    fn: Callable[..., Any]
    parameters: dict[str, Any]
    description: str

    async def execute(self, call: ToolCall, ctx: TurnContext | None = None) -> ToolResult:
        result = await self.fn(**call.arguments)
        if isinstance(result, ToolResult):
            return result
        return ToolResult.ok(str(result))

    async def execute_stream(
        self, call: ToolCall, ctx: TurnContext | None = None
    ) -> AsyncIterator[ToolProgressDelta | ToolResult]:
        """Streaming execution: yields progress deltas, finishes with ToolResult.

        Falls back to non-streaming execute() if the function doesn't
        return an async generator.
        """
        result_or_iter = self.fn(**call.arguments)
        if inspect.isasyncgen(result_or_iter):
            # Async generator — stream each chunk
            collected: list[str] = []
            try:
                async for chunk in result_or_iter:
                    if isinstance(chunk, ToolProgressDelta):
                        yield chunk
                        collected.append(chunk.text)
                    else:
                        text = str(chunk)
                        collected.append(text)
                        yield ToolProgressDelta(
                            id=call.id,
                            name=self.name,
                            text=text,
                        )
            except Exception as e:
                yield ToolResult.error(f"{self.name} failed: {e!r}")
                return
            yield ToolResult.ok("".join(collected) or "(no output)")
            return

        # Non-streaming — resolve and wrap
        result = await result_or_iter
        if isinstance(result, ToolResult):
            yield result
        else:
            yield ToolResult.ok(str(result))


# ---------------------------------------------------------------------------
# Schema inference via Pydantic v2
# ---------------------------------------------------------------------------


def _infer_schema_from_signature(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build an OpenAI function-calling JSON Schema from ``fn``'s signature.

    Uses ``Annotated[T, Field(description=..., ...)]`` metadata for
    per-parameter descriptions and constraints.
    """
    sig = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)

    fields: dict[str, tuple[Any, Any]] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "ctx"):
            continue
        annotation = hints.get(param_name, str)

        # Extract Annotated metadata if present
        if hasattr(annotation, "__metadata__"):
            base_type = annotation.__origin__
            metadata = annotation.__metadata__
            field_info: FieldInfo | None = None
            for m in metadata:
                if isinstance(m, FieldInfo):
                    field_info = m
                    break
            if field_info is not None:
                fields[param_name] = (base_type, field_info)
            else:
                fields[param_name] = (base_type, ...)
        else:
            has_default = param.default is not inspect.Parameter.empty
            default = param.default if has_default else ...
            fields[param_name] = (annotation, default)

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    model = create_model(f"{fn.__name__}_Params", **fields)
    schema = model.model_json_schema()
    schema["required"] = required
    return schema


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------

# Module-level registry for auto-discovery (similar to Hermes)
_registry: dict[str, Tool] = {}


def tool(
    name: str,
    *,
    description: str = "",
) -> Callable[[Callable[..., Any]], FunctionTool]:
    """Register an async function as a Tool.

    Usage::

        @tool("read_file", description="Read a file from disk")
        async def read_file(
            path: Annotated[str, Field(description="File path")],
        ) -> ToolResult: ...

    Async generators are auto-detected and get streaming support::

        @tool("terminal", description="Run a shell command")
        async def terminal(command: str) -> AsyncIterator[str]:
            proc = await asyncio.create_subprocess_shell(...)
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                yield line.decode()

    The returned ``FunctionTool`` is also stored in the module-level
    ``_registry`` for ``_default_builtins()`` discovery.
    """

    def decorator(fn: Callable[..., Any]) -> FunctionTool:
        desc = description or (fn.__doc__ or "").strip().split("\n")[0]
        params = _infer_schema_from_signature(fn)
        ft = FunctionTool(name=name, fn=fn, parameters=params, description=desc)
        _registry[name] = ft
        return ft

    return decorator


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Manages a collection of tools, provides lookup and schema export."""

    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        if tools:
            for t in tools:
                self.register(t)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools.keys())

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Export all tools in OpenAI ``tools`` parameter format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def execute(self, call: ToolCall, ctx: TurnContext | None = None) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult.error(f"unknown tool: {call.name}")
        return await tool.execute(call, ctx)

    async def execute_stream(
        self, call: ToolCall, ctx: TurnContext | None = None
    ) -> AsyncIterator[ToolProgressDelta | ToolResult]:
        """Streaming execution: yields progress deltas, finishes with ToolResult.

        Falls back to non-streaming if the tool doesn't support it.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            yield ToolResult.error(f"unknown tool: {call.name}")
            return

        if hasattr(tool, "execute_stream"):
            async for event in tool.execute_stream(call, ctx):  # type: ignore[union-attr]
                yield event
        else:
            result = await tool.execute(call, ctx)
            yield result


def _default_builtins() -> list[Tool]:
    """Collect all tools registered via @tool in builtins/.

    Each import triggers @tool decorator side-effects that populate
    the module-level _registry. The aliases (_rf, _bash, ...) suppress
    the "imported but unused" lint warning while making the side-effect
    intent explicit.
    """
    from ..tools.builtins import bash as _bash  # noqa: F401
    from ..tools.builtins import browser as _br  # noqa: F401
    from ..tools.builtins import context7 as _c7  # noqa: F401
    from ..tools.builtins import edit_file as _ef  # noqa: F401
    from ..tools.builtins import execute_code as _ec  # noqa: F401
    from ..tools.builtins import file_tree as _ft  # noqa: F401
    from ..tools.builtins import git as _git  # noqa: F401
    from ..tools.builtins import glob as _glob  # noqa: F401
    from ..tools.builtins import grep as _grep  # noqa: F401
    from ..tools.builtins import process as _pr  # noqa: F401
    from ..tools.builtins import read_file as _rf  # noqa: F401
    from ..tools.builtins import session_search as _ss  # noqa: F401
    from ..tools.builtins import skill_manage as _sm  # noqa: F401
    from ..tools.builtins import task as _task  # noqa: F401
    from ..tools.builtins import todo_plan_exit as _tpe  # noqa: F401
    from ..tools.builtins import vision_analyze as _va  # noqa: F401
    from ..tools.builtins import web_fetch as _wfetch  # noqa: F401
    from ..tools.builtins import web_search as _ws  # noqa: F401
    from ..tools.builtins import write_file as _wf  # noqa: F401

    return list(_registry.values())


# ---------------------------------------------------------------------------
# Toolset layering — core / extended / scene
# ---------------------------------------------------------------------------

TOOLSETS: dict[str, frozenset[str]] = {
    "core": frozenset({
        "read_file",
        "write_file",
        "edit_file",
        "grep",
        "glob",
        "bash",
        "task",
    }),
    "extended": frozenset({
        "web_search",
        "web_fetch",
        "context7",
        "session_search",
        "todo",
        "plan",
        "exit",
        "skill_manage",
        "process",
    }),
    "scene": frozenset({
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "execute_code",
        "vision_analyze",
    }),
}


def resolve_toolset(spec: str) -> set[str]:
    """Resolve a comma-separated toolset spec into a set of tool names.

    Example: "core,extended" → union of core and extended tool names.
    Unknown layers are silently ignored (empty contribution).
    """
    result: set[str] = set()
    for layer in spec.split(","):
        layer = layer.strip()
        if layer in TOOLSETS:
            result |= TOOLSETS[layer]
    return result


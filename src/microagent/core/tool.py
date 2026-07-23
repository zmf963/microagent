"""Tool Protocol, ToolRegistry, and @tool decorator.

Tools are the agent's way of interacting with the world. Every tool
implements the ``Tool`` Protocol (PEP 544 structural subtyping) — no
base class inheritance required.

Schema inference for ``@tool`` decorated functions uses Pydantic v2:
parameter type annotations (via ``Annotated[T, Field(...)]``) are
compiled into an OpenAI-compatible JSON Schema.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable, get_type_hints

from pydantic import Field, create_model
from pydantic.fields import FieldInfo

from .types import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# TurnContext forward reference (minimal in M0a — full version in M0b)
# ---------------------------------------------------------------------------

# M0a: tools don't need context. M0b will replace this with the full
# TurnContext dataclass (session_id, history, budget, config, ...).
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

    async def execute(
        self, call: ToolCall, ctx: TurnContext | None = None
    ) -> ToolResult: ...


# ---------------------------------------------------------------------------
# FunctionTool — wraps a plain async function into a Tool
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FunctionTool:
    """Adapter that wraps an ``async def`` function into a Tool."""
    name: str
    fn: Callable[..., Any]
    parameters: dict[str, Any]
    description: str

    async def execute(
        self, call: ToolCall, ctx: TurnContext | None = None
    ) -> ToolResult:
        result = await self.fn(**call.arguments)
        if isinstance(result, ToolResult):
            return result
        # Auto-wrap raw strings
        return ToolResult.ok(str(result))


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
        ) -> ToolResult:
            ...

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

    async def execute(
        self, call: ToolCall, ctx: TurnContext | None = None
    ) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult.error(f"unknown tool: {call.name}")
        return await tool.execute(call, ctx)


def _default_builtins() -> list[Tool]:
    """Collect all tools registered via @tool in builtins/."""
    # Import builtins modules to trigger @tool registration
    from ..tools.builtins import read_file as _rf  # noqa: F401
    from ..tools.builtins import bash as _bash  # noqa: F401
    from ..tools.builtins import write_file as _wf  # noqa: F401
    from ..tools.builtins import edit_file as _ef  # noqa: F401
    from ..tools.builtins import grep as _grep  # noqa: F401
    from ..tools.builtins import glob as _glob  # noqa: F401
    from ..tools.builtins import web_fetch as _wfetch  # noqa: F401
    from ..tools.builtins import todo_plan_exit as _tpe  # noqa: F401
    from ..tools.builtins import task as _task  # noqa: F401
    from ..tools.builtins import skill_manage as _sm  # noqa: F401
    from ..tools.builtins import web_search as _ws  # noqa: F401
    from ..tools.builtins import execute_code as _ec  # noqa: F401
    from ..tools.builtins import vision_analyze as _va  # noqa: F401

    return list(_registry.values())

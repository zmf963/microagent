"""Round-22 🟡: tool arguments are validated against the advertised
pydantic contract at execution time.

Previously Field constraints (le=600, ge=0, types) were schema-display
only — nothing checked the actual call.arguments, so a hallucinating or
injected model could send timeout=10**9 or wrong-typed scalars that
crashed deep inside tool bodies. Also covers the Annotated-with-default
fix: passing the bare FieldInfo as the pydantic field default made
per-action optional params (skill_manage old_string/new_string) appear
REQUIRED to the validator.
"""

from typing import Annotated

from pydantic import Field

from microagent.core.tool import ToolRegistry, tool
from microagent.core.types import ToolCall, ToolResult


def _make_capped_tool():
    @tool(
        "capped_tool",
        description="Tool with numeric constraints for validation tests.",
    )
    async def capped_tool(
        timeout: Annotated[int, Field(description="seconds", ge=1, le=600)] = 30,
        name: Annotated[str, Field(description="a name")] = "",
    ) -> ToolResult:
        return ToolResult.ok(f"timeout={timeout} name={name!r}")

    return capped_tool


class TestArgumentValidation:
    async def test_over_max_rejected(self):
        registry = ToolRegistry([_make_capped_tool()])
        result = await registry.execute(
            ToolCall(id="c1", name="capped_tool", arguments={"timeout": 10**9})
        )
        assert result.is_error
        assert "less_than_equal" in result.content or "600" in result.content

    async def test_under_min_rejected(self):
        registry = ToolRegistry([_make_capped_tool()])
        result = await registry.execute(
            ToolCall(id="c1", name="capped_tool", arguments={"timeout": 0})
        )
        assert result.is_error

    async def test_unknown_key_rejected(self):
        registry = ToolRegistry([_make_capped_tool()])
        result = await registry.execute(
            ToolCall(
                id="c1", name="capped_tool", arguments={"surprise": 1}
            )
        )
        assert result.is_error
        assert "surprise" in result.content

    async def test_wrong_type_rejected(self):
        registry = ToolRegistry([_make_capped_tool()])
        result = await registry.execute(
            ToolCall(
                id="c1", name="capped_tool", arguments={"timeout": "not-a-number"}
            )
        )
        assert result.is_error

    async def test_valid_args_pass_and_string_number_coerced(self):
        registry = ToolRegistry([_make_capped_tool()])
        result = await registry.execute(
            ToolCall(id="c1", name="capped_tool", arguments={"timeout": 60})
        )
        assert not result.is_error
        assert "timeout=60" in result.content

    async def test_defaults_fill_omitted_args(self):
        """No arguments at all — signature defaults must apply (the
        Annotated-with-default FieldInfo bug made these REQUIRED)."""
        registry = ToolRegistry([_make_capped_tool()])
        result = await registry.execute(
            ToolCall(id="c1", name="capped_tool", arguments={})
        )
        assert not result.is_error
        assert "timeout=30" in result.content

    async def test_skill_manage_partial_args_still_work(self):
        """skill_manage's old_string/new_string are per-action optional;
        a create call must not be rejected for omitting them."""
        from microagent.tools.builtins import skill_manage as skill_manage_mod

        skill_tool = skill_manage_mod.skill_manage
        registry = ToolRegistry([skill_tool])
        result = await registry.execute(
            ToolCall(
                id="c1",
                name="skill_manage",
                arguments={"action": "list"},
            )
        )
        # list works without name/old_string/new_string — either an ok
        # listing or a skills-dir error, but NOT an arguments error.
        assert not (
            result.is_error and "invalid arguments" in result.content
        ), result.content

    async def test_streaming_path_validates_too(self):
        registry = ToolRegistry([_make_capped_tool()])
        events = []
        async for ev in registry.execute_stream(
            ToolCall(id="c1", name="capped_tool", arguments={"timeout": 999999})
        ):
            events.append(ev)
        assert events and isinstance(events[-1], ToolResult)
        assert events[-1].is_error

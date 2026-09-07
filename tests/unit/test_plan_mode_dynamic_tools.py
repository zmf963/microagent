"""Round-22 🟡: plan mode was bypassable via mid-session MCP tool
registration. _get_available_tools subtracted only the static blocklist,
and _settle's guard checked the same static set — an MCP tool registered
while in build mode stayed callable (and advertised) after flipping to
plan, executing arbitrary write operations under the read-only guarantee.
"""

from microagent.core.tool import ToolRegistry
from microagent.core.types import Message, ToolResult, ToolResultDelta
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner

from .fake_llm import FakeLLMClient, text_response, tool_response


class _MCPStyleAdapter:
    """Mimics mcp.client.MCPToolAdapter: implements the Tool Protocol but
    is NOT a FunctionTool — that's how dynamically registered MCP tools
    slip past a builtin-name allowlist check."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"dynamic {name}"
        self.parameters: dict = {}
        self.executed = 0

    async def execute(self, call, ctx=None):  # noqa: ANN001
        self.executed += 1
        return ToolResult.ok("mcp side effect done")


def _make_read_tool():
    from microagent.core.tool import tool

    @tool("plan_probe_read", description="read-only probe")
    async def plan_probe_read() -> ToolResult:
        return ToolResult.ok("read ok")

    return plan_probe_read


class TestPlanModeDynamicToolBypass:
    async def test_dynamic_tools_hidden_in_plan_mode(self):
        adapter = _MCPStyleAdapter("mcp_write_thing")
        registry = ToolRegistry([_make_read_tool(), adapter])
        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=registry,
            budget=Budget(),
        )
        runner.mode = "plan"
        available = runner._get_available_tools()
        assert "plan_probe_read" in available  # builtin read tool stays
        assert "mcp_write_thing" not in available  # dynamic adapter hidden

    async def test_dynamic_tool_call_denied_at_execution(self):
        adapter = _MCPStyleAdapter("mcp_write_thing")
        registry = ToolRegistry([_make_read_tool(), adapter])
        llm = FakeLLMClient(
            [
                tool_response([("c1", "mcp_write_thing", {})]),
                text_response("done"),
            ]
        )
        runner = SessionRunner(llm=llm, registry=registry, budget=Budget())
        runner.mode = "plan"
        events = []
        async for ev in runner.run_turn([Message.user("go")]):
            events.append(ev)
        results = [e for e in events if isinstance(e, ToolResultDelta)]
        assert results and results[0].is_error
        assert "not available in plan mode" in results[0].content
        assert adapter.executed == 0  # never ran

    async def test_build_mode_unaffected(self):
        adapter = _MCPStyleAdapter("mcp_write_thing")
        registry = ToolRegistry([_make_read_tool(), adapter])
        runner = SessionRunner(
            llm=FakeLLMClient([text_response("ok")]),
            registry=registry,
            budget=Budget(),
        )
        assert "mcp_write_thing" in runner._get_available_tools()

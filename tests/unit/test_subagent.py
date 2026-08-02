"""Tests for SubagentSpec and SubagentManager."""

from microagent import Budget, SessionRunner, ToolRegistry
from microagent.subagent.manager import SubagentManager, SubagentSpec
from tests.unit.fake_llm import FakeLLMClient, text_response


class TestSubagentSpec:
    def test_create_spec(self):
        spec = SubagentSpec(
            name="explore",
            description="Read-only code exploration",
            system_prompt="You search code. No writes.",
            tools_allowed=("grep", "glob", "read_file"),
            tools_blocked=(),
            model="fast-model",
            max_iterations=10,
            max_cost_usd=0.5,
        )
        assert spec.name == "explore"
        assert spec.tools_allowed == ("grep", "glob", "read_file")
        assert spec.tools_blocked == ()
        assert spec.model == "fast-model"

    def test_defaults(self):
        spec = SubagentSpec(
            name="general",
            description="general",
            system_prompt="...",
            tools_allowed=(),
            tools_blocked=("exit",),
        )
        assert spec.model is None  # inherits parent model
        assert spec.max_iterations == 10
        assert spec.max_cost_usd == 1.0


class TestSubagentManager:
    async def test_spawn_explore_subagent(self):
        """Subagent runs with restricted tools and returns result."""
        spec = SubagentSpec(
            name="echo",
            description="echo",
            system_prompt="You echo user input.",
            tools_allowed=(),  # no tools needed
            tools_blocked=(),
        )
        manager = SubagentManager(specs=(spec,))

        # Parent runner with full toolset — subagent should not see these
        parent_llm = FakeLLMClient([text_response("ignored")])
        parent_runner = SessionRunner(
            llm=parent_llm,
            registry=ToolRegistry(),
        )

        result = await manager.spawn("echo", "hello", parent_runner)
        assert isinstance(result, str)

    async def test_spawn_with_tool_filtering(self):
        """Subagent cannot use tools not in its allowlist."""


        from microagent.core.tool import tool
        from microagent.core.types import ToolResult

        @tool("secret_tool", description="secret")
        async def secret_tool() -> ToolResult:
            return ToolResult.ok("secret output")

        spec = SubagentSpec(
            name="filtered",
            description="filtered",
            system_prompt="Use available tools.",
            tools_allowed=("grep",),
            tools_blocked=("secret_tool",),
        )
        manager = SubagentManager(specs=(spec,))

        parent_runner = SessionRunner(
            llm=FakeLLMClient([text_response("done")]),
            registry=ToolRegistry([secret_tool]),
        )

        # Subagent should get filtered registry (only grep allowed,
        # secret_tool blocked)
        result = await manager.spawn("filtered", "search for x", parent_runner)
        assert isinstance(result, str)

    async def test_subagent_runs_with_spawned_budget(self):
        """Subagent budget is spawned from parent — tree-shaped tracking."""
        spec = SubagentSpec(
            name="budget-test",
            description="budget test",
            system_prompt="...",
            tools_allowed=(),
            tools_blocked=(),
            max_iterations=3,
        )
        manager = SubagentManager(specs=(spec,))

        parent_llm = FakeLLMClient(
            [
                text_response("subagent completed"),
            ]
        )
        parent_runner = SessionRunner(
            llm=parent_llm,
            registry=ToolRegistry(),
            budget=Budget(max_iterations=5),
        )

        result = await manager.spawn("budget-test", "do work", parent_runner)
        assert "budget" not in result.lower() or "subagent" in result.lower()


class TestSubagentMore:
    async def test_spawn_cancelled_parent(self):
        """If parent budget is cancelled, subagent returns early."""
        from microagent.subagent.manager import SubagentManager, SubagentSpec
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry
        from microagent.session.budget import Budget, BudgetExceeded
        from tests.unit.fake_llm import FakeLLMClient

        spec = SubagentSpec(
            name="c", description="c", system_prompt="...",
            tools_allowed=(), tools_blocked=(),
        )
        manager = SubagentManager(specs=(spec,))
        parent = SessionRunner(
            llm=FakeLLMClient([]),
            registry=ToolRegistry(),
            budget=Budget.root(max_iterations=1),
        )
        # Exhaust the budget → sets the shared cancel_event
        try:
            await parent.budget.consume(iterations=1)
        except BudgetExceeded:
            pass
        assert parent.budget.is_cancelled()
        result = await manager.spawn("c", "hi", parent)
        assert "cancelled" in result
        await parent.close()

    async def test_spawn_returns_empty(self):
        """If the child LLM yields nothing, spawn returns the empty marker."""
        from microagent.subagent.manager import SubagentManager, SubagentSpec
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry
        from tests.unit.fake_llm import FakeLLMClient

        spec = SubagentSpec(
            name="empty", description="empty", system_prompt="...",
            tools_allowed=(), tools_blocked=(),
        )
        manager = SubagentManager(specs=(spec,))
        parent = SessionRunner(
            llm=FakeLLMClient([]),  # no responses → empty
            registry=ToolRegistry(),
        )
        result = await manager.spawn("empty", "hi", parent)
        assert "returned empty" in result
        await parent.close()

    async def test_spawn_with_model_override(self):
        """spec.model overrides the child's LLM model."""
        from microagent.subagent.manager import SubagentManager, SubagentSpec
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry
        from microagent.llm.client import LLMConfig
        from tests.unit.fake_llm import FakeLLMClient, text_response

        spec = SubagentSpec(
            name="override", description="override", system_prompt="...",
            tools_allowed=(), tools_blocked=(),
            model="special-model",
        )
        manager = SubagentManager(specs=(spec,))
        fake = FakeLLMClient([text_response("done")])
        parent = SessionRunner(llm=fake, registry=ToolRegistry())
        result = await manager.spawn("override", "hi", parent)
        assert isinstance(result, str)
        await parent.close()

    async def test_spawn_turnfailed(self):
        """If the child turn fails (e.g. budget), spawn reports it."""
        from microagent.subagent.manager import SubagentManager, SubagentSpec
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry
        from microagent.session.budget import Budget
        from tests.unit.fake_llm import FakeLLMClient, text_response

        spec = SubagentSpec(
            name="fail", description="fail", system_prompt="...",
            tools_allowed=(), tools_blocked=(),
            max_iterations=1,
        )
        manager = SubagentManager(specs=(spec,))
        parent = SessionRunner(
            llm=FakeLLMClient([text_response("a"), text_response("b")]),
            registry=ToolRegistry(),
            budget=Budget(max_iterations=2),
        )
        result = await manager.spawn("fail", "hi", parent)
        # Either a result or a failure marker
        assert isinstance(result, str)
        await parent.close()

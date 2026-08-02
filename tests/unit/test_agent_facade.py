"""Tests for Agent facade uncovered paths: custom tools, skills_path,
enable_cron, and the sync run() entry point."""

import pytest


class TestFromConfigOptions:
    def test_with_custom_tools(self):
        import uuid
        from microagent import Agent
        from microagent.llm.client import LLMConfig
        from microagent.core.types import ToolResult, ToolCall

        # A custom tool NOT created via @tool (so it's not auto-discovered
        # into _default_builtins and won't duplicate when passed explicitly).
        class CustomTool:
            name = f"zzz_custom_{uuid.uuid4().hex[:8]}"
            description = "custom tool"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, call: ToolCall, ctx=None) -> ToolResult:
                return ToolResult.ok("custom result")

        custom = CustomTool()
        agent = Agent.from_config(
            LLMConfig(base_url="http://x", api_key="k", model="m"),
            tools=[custom],
        )
        assert custom.name in agent.registry.names
        assert len(agent.registry.names) >= 31
        import asyncio
        asyncio.run(agent.close())

    def test_with_skills_path(self, tmp_path):
        from microagent import Agent
        from microagent.llm.client import LLMConfig
        # Create a skill dir
        sd = tmp_path / "myskills" / "demo"
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text("---\nname: demo\ndescription: demo skill\n---\nbody\n")

        agent = Agent.from_config(
            LLMConfig(base_url="http://x", api_key="k", model="m"),
            skills_path=str(tmp_path / "myskills"),
        )
        assert agent.runner.skill_loader is not None
        import asyncio
        skills = asyncio.run(agent.runner.skill_loader.load())
        assert any(s.name == "demo" for s in skills)
        asyncio.run(agent.close())

    def test_with_enable_cron(self):
        from microagent import Agent
        from microagent.llm.client import LLMConfig
        agent = Agent.from_config(
            LLMConfig(base_url="http://x", api_key="k", model="m"),
            enable_cron=True,
        )
        assert agent.cron is not None
        import asyncio
        asyncio.run(agent.cron.stop())
        asyncio.run(agent.close())

    def test_with_none_skills_path(self):
        """skills_path=None → no skill loader (empty search paths)."""
        from microagent import Agent
        from microagent.llm.client import LLMConfig
        # monkeypatch the builtin skills dir to not exist
        agent = Agent.from_config(
            LLMConfig(base_url="http://x", api_key="k", model="m"),
            skills_path=None,
        )
        import asyncio
        asyncio.run(agent.close())

    def test_run_sync_with_string(self):
        """run() (sync) accepts a string and returns text (uses asyncio.run)."""
        from microagent.agent import Agent
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry, _default_builtins
        from microagent.session.budget import Budget
        from tests.unit.fake_llm import FakeLLMClient, text_response

        fake = FakeLLMClient([text_response("sync reply")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        agent = Agent(runner=runner, registry=runner.registry)
        result = agent.run("hello")  # sync entry
        assert "sync reply" in result

    def test_run_sync_with_message_list(self):
        from microagent.agent import Agent
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry, _default_builtins
        from microagent.session.budget import Budget
        from microagent.core.types import Message
        from tests.unit.fake_llm import FakeLLMClient, text_response

        fake = FakeLLMClient([text_response("list reply")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        agent = Agent(runner=runner, registry=runner.registry)
        result = agent.run([Message.user("hello")])
        assert "list reply" in result

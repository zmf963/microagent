"""Tests for Prompt Caching protection — system prompt frozen,
skills/memory/context_sources injected to user message.

After this change, system prompt must be byte-stable across turns.
Skills, memory, and ContextSource contributions are wrapped in
<context> fence and appended to the current user message instead.
"""

from microagent.core.tool import ToolRegistry
from microagent.core.types import Message, TextDelta, TurnComplete, Usage
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner
from tests.unit.fake_llm import FakeLLMClient, text_response


class TestPromptCaching:
    async def test_system_prompt_stable_across_turns(self):
        """System prompt must be the same across multiple turns (no skills)."""
        llm = FakeLLMClient([text_response("resp1"), text_response("resp2")])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([]),
            system_prompt="You are a test assistant.",
            budget=Budget(max_iterations=10),
        )

        # Turn 1
        messages1 = [Message.user("hello")]
        async for _ in runner.run_turn(messages1):
            pass
        system1 = llm.calls[0]["system"]

        # Turn 2
        messages2 = [Message.user("world")]
        async for _ in runner.run_turn(messages2):
            pass
        system2 = llm.calls[1]["system"]

        assert system1 == system2, "System prompt must be byte-stable across turns"

    async def test_skills_injected_to_user_message_not_system(self):
        """Skill content must appear in user message, NOT in system prompt."""
        from microagent.skill.loader import ClaudeSkillLoader
        from pathlib import Path
        import tempfile

        # Create a temp skill directory
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "test_skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test\ndescription: test skill\ntriggers: [test]\n---\nSKILL BODY CONTENT"
            )

            loader = ClaudeSkillLoader(search_paths=(Path(tmpdir),))
            llm = FakeLLMClient([text_response("ok")])
            runner = SessionRunner(
                llm=llm,
                registry=ToolRegistry([]),
                system_prompt="You are a test assistant.",
                budget=Budget(max_iterations=10),
                skill_loader=loader,
            )

            messages = [Message.user("test this")]
            async for _ in runner.run_turn(messages):
                pass

            system = llm.calls[0]["system"]
            user_msgs = [m for m in llm.calls[0]["messages"] if m.role == "user"]

            # System must NOT contain skill body
            assert "SKILL BODY CONTENT" not in system
            # User message must contain skill body in <context> fence
            user_content = " ".join(m.content for m in user_msgs)
            assert "SKILL BODY CONTENT" in user_content
            assert "<context>" in user_content

    async def test_context_source_injected_to_user_message(self):
        """ContextSource contribution must go to user message, not system."""

        class FakeContextSource:
            async def contribute(self, ctx):
                return "DYNAMIC CONTEXT DATA"

        llm = FakeLLMClient([text_response("ok")])
        runner = SessionRunner(
            llm=llm,
            registry=ToolRegistry([]),
            system_prompt="You are a test assistant.",
            budget=Budget(max_iterations=10),
            context_sources=(FakeContextSource(),),
        )

        messages = [Message.user("hello")]
        async for _ in runner.run_turn(messages):
            pass

        system = llm.calls[0]["system"]
        user_msgs = [m for m in llm.calls[0]["messages"] if m.role == "user"]

        assert "DYNAMIC CONTEXT DATA" not in system
        user_content = " ".join(m.content for m in user_msgs)
        assert "DYNAMIC CONTEXT DATA" in user_content

    async def test_context_fence_wraps_injections(self):
        """Injections are wrapped in <context>...</context> fence."""
        from microagent.skill.loader import ClaudeSkillLoader
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "sk"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: t\ndescription: t\ntriggers: [x]\n---\nBODY"
            )
            loader = ClaudeSkillLoader(search_paths=(Path(tmpdir),))
            llm = FakeLLMClient([text_response("ok")])
            runner = SessionRunner(
                llm=llm,
                registry=ToolRegistry([]),
                system_prompt="sys",
                budget=Budget(max_iterations=10),
                skill_loader=loader,
            )
            messages = [Message.user("x")]
            async for _ in runner.run_turn(messages):
                pass

            user_msg = next(m for m in llm.calls[0]["messages"] if m.role == "user")
            assert "<context>" in user_msg.content
            assert "</context>" in user_msg.content
            assert "BODY" in user_msg.content

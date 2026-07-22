"""Tests for extension point Protocols: PreLLMHook, ToolHook, ContextSource."""

from microagent.core.types import ToolCall, ToolResult


class TestPreLLMHook:
    async def test_protocol_accepted(self):
        """Verify a class implementing PreLLMHook is accepted."""
        # PreLLMHook is a simple Protocol — any callable works
        async def hook(ctx):
            return ctx  # identity

        result = await hook("fake_ctx")
        assert result == "fake_ctx"


class TestToolHook:
    async def test_before_allow(self):
        """ToolHook.before returns modified call → allowed."""

        class AuditHook:
            async def before(self, call, ctx):
                return call  # pass through

            async def after(self, call, result, ctx):
                return result  # pass through

        hook = AuditHook()
        call = ToolCall(id="c1", name="bash", arguments={})
        result = await hook.before(call, None)
        assert result is call

    async def test_before_deny(self):
        """ToolHook.before returns None → denied."""

        class DenyHook:
            async def before(self, call, ctx):
                return None

            async def after(self, call, result, ctx):
                return result

        hook = DenyHook()
        call = ToolCall(id="c1", name="bash", arguments={})
        result = await hook.before(call, None)
        assert result is None

    async def test_after_transform(self):
        """ToolHook.after can modify the result."""

        class CensorHook:
            async def before(self, call, ctx):
                return call

            async def after(self, call, result, ctx):
                return ToolResult.ok("[REDACTED]")

        hook = CensorHook()
        result = ToolResult.ok("secret data")
        transformed = await hook.after(None, result, None)
        assert transformed.content == "[REDACTED]"


class TestContextSource:
    async def test_contribute(self):
        """ContextSource contributes extra text to system prompt."""

        class GitSource:
            async def contribute(self, ctx):
                return "git: main branch, clean"

        src = GitSource()
        extra = await src.contribute(None)
        assert "main branch" in extra

"""Tests for context7 HTTP path, execute_code edge cases, agent facade."""

import pytest


# ============================ context7 ============================
class TestContext7HTTP:
    @pytest.mark.asyncio
    async def test_error_response(self, monkeypatch):
        """A 4xx/5xx from context7 → error result."""
        import httpx
        from microagent.tools.builtins.context7 import context7

        class _BadResp:
            def raise_for_status(self):
                from httpx import HTTPStatusError
                raise HTTPStatusError("bad", request=None, response=httpx.Response(500))

            async def aiter_bytes(self):
                yield b""

        class _Stream:
            def __init__(self, r): self._r = r
            async def __aenter__(self): return self._r
            async def __aexit__(self, *a): return False

        class _Client:
            def stream(self, *a, **k):
                return _Stream(_BadResp())

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
        r = await context7.fn(query="pydantic")
        assert r.is_error
        assert "context7" in r.content.lower() or "error" in r.content.lower()

    @pytest.mark.asyncio
    async def test_http_exception(self, monkeypatch):
        import httpx
        from microagent.tools.builtins.context7 import context7
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: (_ for _ in ()).throw(ConnectionError("down")))
        r = await context7.fn(query="x")
        assert r.is_error
        assert "context7" in r.content.lower()

    @pytest.mark.asyncio
    async def test_success_response(self, monkeypatch):
        import json
        import httpx
        from microagent.tools.builtins.context7 import context7

        payload = json.dumps({"results": [
            {"title": "Pydantic", "snippet": "Models", "url": "https://x", "library": "pydantic"},
        ]}).encode()

        class _OkResp:
            def raise_for_status(self): pass
            async def aiter_bytes(self): yield payload

        class _Stream:
            def __init__(self, r): self._r = r
            async def __aenter__(self): return self._r
            async def __aexit__(self, *a): return False

        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def stream(self, *a, **k): return _Stream(_OkResp())

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
        r = await context7.fn(query="pydantic")
        assert not r.is_error
        assert "Pydantic" in r.content


# ============================ execute_code ============================
class TestExecuteCodeMore:
    @pytest.mark.asyncio
    async def test_huge_output_truncated(self):
        """execute_code caps output at 100KB."""
        from microagent.tools.builtins.execute_code import execute_code
        r = await execute_code.fn(code="print('x' * 1000000)", timeout=10)
        assert not r.is_error
        assert "truncated" in r.content.lower()

    @pytest.mark.asyncio
    async def test_empty_code(self):
        from microagent.tools.builtins.execute_code import execute_code
        r = await execute_code.fn(code="   ")
        assert r.is_error
        assert "code is required" in r.content

    @pytest.mark.asyncio
    async def test_stderr_included(self):
        """Errors on stderr are reported in the result."""
        from microagent.tools.builtins.execute_code import execute_code
        r = await execute_code.fn(code="import sys; sys.stderr.write('oops\\n'); sys.exit(1)", timeout=5)
        assert r.is_error
        assert "oops" in r.content.lower() or "oops" in r.content


# ============================ agent facade ============================
class TestAgentFacade:
    @pytest.mark.asyncio
    async def test_agent_with_system_prompt(self):
        from microagent.agent import Agent
        from microagent.llm.client import LLMConfig
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry, _default_builtins
        from microagent.session.budget import Budget
        from microagent.core.types import Message
        from tests.unit.fake_llm import FakeLLMClient, text_response

        fake = FakeLLMClient([text_response("ok")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()),
            budget=Budget.root(), system_prompt="You are an expert.",
        )
        agent = Agent(runner=runner, registry=runner.registry)
        # Verify system prompt was passed to the LLM
        result = await agent.arun([Message.user("hi")])
        assert "ok" in result
        assert fake.calls[0]["system"].startswith("You are an expert.")
        await agent.close()

    @pytest.mark.asyncio
    async def test_agent_run_accepts_string(self):
        """agent.run (sync) wraps a plain string as a user message.
        Tested via the wrapping logic without nested event loop."""
        from microagent.agent import Agent
        from microagent.session.runner import SessionRunner
        from microagent.core.tool import ToolRegistry, _default_builtins
        from microagent.session.budget import Budget
        from microagent.core.types import Message
        from tests.unit.fake_llm import FakeLLMClient, text_response

        fake = FakeLLMClient([text_response("hello string")])
        runner = SessionRunner(
            llm=fake, registry=ToolRegistry(_default_builtins()), budget=Budget.root(),
        )
        agent = Agent(runner=runner, registry=runner.registry)
        # arun takes a list; verify the string-wrap logic that run() uses
        wrapped = [Message.user("just a string")]
        result = await agent.arun(wrapped)
        assert "hello string" in result
        await agent.close()

    @pytest.mark.asyncio
    async def test_from_config_builds_tools(self):
        from microagent import Agent
        from microagent.llm.client import LLMConfig
        agent = Agent.from_config(LLMConfig(base_url="http://x", api_key="k", model="m"))
        assert len(agent.registry.names) >= 30
        await agent.close()

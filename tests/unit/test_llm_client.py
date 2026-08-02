"""Tests for CredentialPool rotation and OpenAIChatClient static logic."""

import pytest

from microagent.llm.client import LLMConfig, OpenAIChatClient
from microagent.llm.pool import CredentialPool
from microagent.core.types import Message


def _cfg(model="m"):
    return LLMConfig(base_url="http://x", api_key="k", model=model)


class TestCredentialPool:
    def test_single_credential(self):
        pool = CredentialPool(credentials=(_cfg(),))
        assert pool.current.model == "m"

    def test_rotation(self):
        pool = CredentialPool(credentials=(_cfg("m1"), _cfg("m2"), _cfg("m3")))
        assert pool.current.model == "m1"
        pool.next()
        assert pool.current.model == "m2"
        pool.next()
        assert pool.current.model == "m3"
        pool.next()  # wraps around
        assert pool.current.model == "m1"

    def test_mark_failed_rotates(self):
        pool = CredentialPool(credentials=(_cfg("m1"), _cfg("m2")))
        pool.mark_failed()
        assert pool.current.model == "m2"

    def test_mark_failed_resets_after_all(self):
        """After all keys exhausted, mark_failed resets to first (no next() after reset)."""
        pool = CredentialPool(credentials=(_cfg("m1"), _cfg("m2")))
        pool.mark_failed()  # → m2, failed=1
        assert pool.current.model == "m2"
        pool.mark_failed()  # failed=2 >= len=2 → reset to index 0
        assert pool.current.model == "m1"
        assert pool._failed == 0

    def test_mark_failed_single_credential(self):
        pool = CredentialPool(credentials=(_cfg(),))
        pool.mark_failed()  # failed=1 >= 1 → reset
        assert pool.current.model == "m"
        assert pool._failed == 0

    def test_empty_pool_raises(self):
        with pytest.raises(ValueError):
            CredentialPool(credentials=())


class TestClientLogic:
    def test_for_model_creates_new_client(self):
        c = OpenAIChatClient(_cfg("base"))
        c2 = c.for_model("other")
        assert c2.config.model == "other"
        assert c2.config.base_url == c.config.base_url
        assert c2 is not c

    def test_is_retryable_auth(self):
        class _Exc:
            status_code = 401
        assert OpenAIChatClient._is_retryable(_Exc()) is True

    def test_is_retryable_ok(self):
        class _Exc:
            status_code = 200
        assert OpenAIChatClient._is_retryable(_Exc()) is False

    def test_is_retryable_no_status(self):
        assert OpenAIChatClient._is_retryable(ValueError("x")) is False

    def test_is_backoff_retryable_5xx(self):
        class _Exc:
            status_code = 503
        assert OpenAIChatClient._is_backoff_retryable(_Exc()) is True

    def test_is_backoff_retryable_4xx(self):
        class _Exc:
            status_code = 400
        assert OpenAIChatClient._is_backoff_retryable(_Exc()) is False

    def test_is_backoff_retryable_no_status(self):
        assert OpenAIChatClient._is_backoff_retryable(ValueError("x")) is False

    @pytest.mark.asyncio
    async def test_close_with_no_client(self):
        c = OpenAIChatClient(_cfg())
        await c.close()  # must not crash when _client is None

    @pytest.mark.asyncio
    async def test_close_closes_client(self):
        c = OpenAIChatClient(_cfg())
        c._client = _FakeOpenAIClient()
        await c.close()
        assert c._client is None
        assert _FakeOpenAIClient.closed


class _FakeOpenAIClient:
    closed = False

    async def close(self):
        _FakeOpenAIClient.closed = True


class TestOpenAIChatClientStream:
    @pytest.mark.asyncio
    async def test_stream_text_and_usage(self, monkeypatch):
        """stream() yields TextDelta + Usage + StreamDone."""
        import sys, types
        from microagent.llm.client import OpenAIChatClient, LLMConfig, TextDelta, Usage, StreamDone

        class _Delta:
            def __init__(self, content=None, reasoning_content=None, tool_calls=None):
                self.content = content
                self.reasoning_content = reasoning_content
                self.tool_calls = tool_calls

        class _Choice:
            def __init__(self, delta, finish_reason=None):
                self.delta = delta
                self.finish_reason = finish_reason

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5

        class _Chunk:
            def __init__(self, choices=None, usage=None):
                self.choices = choices
                self.usage = usage

        class _Completions:
            def __init__(self):
                self._chunks = [
                    _Chunk([_Choice(_Delta(content="hello"))]),
                    _Chunk([_Choice(_Delta(content=" world"))]),
                    _Chunk([_Choice(_Delta(), finish_reason="stop")], usage=_Usage()),
                ]

            async def create(self, **kw):
                async def _gen():
                    for c in self._chunks:
                        yield c
                return _gen()

        class _Chat:
            def __init__(self):
                self.completions = _Completions()

        class _FakeOpenAI:
            def __init__(self, **kw):
                self.chat = _Chat()
            async def close(self): pass

        # Inject fake openai module
        fake_openai = types.ModuleType("openai")
        fake_openai.AsyncOpenAI = _FakeOpenAI
        monkeypatch.setitem(sys.modules, "openai", fake_openai)

        client = OpenAIChatClient(LLMConfig(base_url="http://x", api_key="k", model="m"))
        events = [e async for e in client.stream(
            system="sys", messages=(Message.user("hi"),))]

        text = [e for e in events if isinstance(e, TextDelta)]
        usage = [e for e in events if isinstance(e, Usage)]
        done = [e for e in events if isinstance(e, StreamDone)]
        assert any(t.text == "hello world" for t in text) or len(text) >= 1
        assert len(usage) == 1
        assert len(done) == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_stream_tool_call_accumulation(self, monkeypatch):
        """Tool call arguments streamed in fragments are accumulated."""
        import sys, types
        from microagent.llm.client import OpenAIChatClient, LLMConfig, ToolCallDelta

        class _TC:
            def __init__(self, index, id="", name="", arguments=""):
                self.index = index
                self.id = id
                self.function = types.SimpleNamespace(name=name, arguments=arguments)

        class _Delta:
            def __init__(self, tool_calls=None):
                self.content = None
                self.reasoning_content = None
                self.tool_calls = tool_calls

        class _Choice:
            def __init__(self, delta):
                self.delta = delta
                self.finish_reason = None

        class _Chunk:
            def __init__(self, delta):
                self.choices = [_Choice(delta)]
                self.usage = None

        class _Completions:
            async def create(self, **kw):
                async def _gen():
                    yield _Chunk(_Delta(tool_calls=[_TC(0, id="call_1", name="bash")]))
                    yield _Chunk(_Delta(tool_calls=[_TC(0, arguments='{"command"')]))
                    yield _Chunk(_Delta(tool_calls=[_TC(0, arguments=': "echo hi"}' )]))
                return _gen()

        class _Chat:
            def __init__(self):
                self.completions = _Completions()

        class _FakeOpenAI:
            def __init__(self, **kw):
                self.chat = _Chat()
            async def close(self): pass

        fake_openai = types.ModuleType("openai")
        fake_openai.AsyncOpenAI = _FakeOpenAI
        monkeypatch.setitem(sys.modules, "openai", fake_openai)

        client = OpenAIChatClient(LLMConfig(base_url="http://x", api_key="k", model="m"))
        events = [e async for e in client.stream(system="s", messages=(Message.user("hi"),))]
        calls = [e for e in events if isinstance(e, ToolCallDelta)]
        assert len(calls) == 1
        assert calls[0].name == "bash"
        assert calls[0].arguments == {"command": "echo hi"}
        await client.close()

    @pytest.mark.asyncio
    async def test_stream_reasoning_content(self, monkeypatch):
        """reasoning_content yields TextDelta with kind='thinking'."""
        import sys, types
        from microagent.llm.client import OpenAIChatClient, LLMConfig, TextDelta

        class _Delta:
            def __init__(self):
                self.content = ""
                self.reasoning_content = "let me think..."
                self.tool_calls = None

        class _Choice:
            def __init__(self):
                self.delta = _Delta()
                self.finish_reason = None

        class _Chunk:
            def __init__(self):
                self.choices = [_Choice()]
                self.usage = None

        class _Completions:
            async def create(self, **kw):
                async def _gen():
                    yield _Chunk()
                return _gen()

        class _Chat:
            def __init__(self):
                self.completions = _Completions()

        class _FakeOpenAI:
            def __init__(self, **kw):
                self.chat = _Chat()
            async def close(self): pass

        fake_openai = types.ModuleType("openai")
        fake_openai.AsyncOpenAI = _FakeOpenAI
        monkeypatch.setitem(sys.modules, "openai", fake_openai)

        client = OpenAIChatClient(LLMConfig(base_url="http://x", api_key="k", model="m"))
        events = [e async for e in client.stream(system="s", messages=(Message.user("hi"),))]
        thinking = [e for e in events if isinstance(e, TextDelta) and e.kind == "thinking"]
        assert len(thinking) == 1
        assert "let me think" in thinking[0].text
        await client.close()

"""Tests for CredentialPool rotation and OpenAIChatClient static logic."""

import pytest

from microagent.llm.client import LLMConfig, OpenAIChatClient
from microagent.llm.pool import CredentialPool


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

"""Tests for jittered_backoff retry in OpenAIChatClient.stream().

When stream() encounters rate-limit (429), timeout, connection error,
or 5xx, it retries with exponential backoff + jitter, up to 3 times.
Auth errors (401) and bad request errors (400) do not retry.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from microagent.llm.client import LLMConfig, OpenAIChatClient


class TestJitteredBackoff:
    def test_is_retryable_rate_limit(self):
        """429 is retryable."""
        from openai import RateLimitError

        exc = RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        assert OpenAIChatClient._is_backoff_retryable(exc) is True

    def test_is_retryable_timeout(self):
        """APITimeoutError is retryable."""
        from openai import APITimeoutError

        exc = APITimeoutError(request=MagicMock())
        assert OpenAIChatClient._is_backoff_retryable(exc) is True

    def test_is_retryable_connection_error(self):
        """APIConnectionError is retryable."""
        from openai import APIConnectionError

        exc = APIConnectionError(request=MagicMock())
        assert OpenAIChatClient._is_backoff_retryable(exc) is True

    def test_is_retryable_internal_server_error(self):
        """5xx is retryable."""
        from openai import InternalServerError

        exc = InternalServerError(
            message="server error",
            response=MagicMock(status_code=503, headers={}),
            body=None,
        )
        assert OpenAIChatClient._is_backoff_retryable(exc) is True

    def test_not_retryable_auth_error(self):
        """401 is NOT retryable by backoff (handled by credential pool)."""
        from openai import AuthenticationError

        exc = AuthenticationError(
            message="bad key",
            response=MagicMock(status_code=401, headers={}),
            body=None,
        )
        assert OpenAIChatClient._is_backoff_retryable(exc) is False

    def test_not_retryable_bad_request(self):
        """400 is NOT retryable."""
        from openai import BadRequestError

        exc = BadRequestError(
            message="bad request",
            response=MagicMock(status_code=400, headers={}),
            body=None,
        )
        assert OpenAIChatClient._is_backoff_retryable(exc) is False

    async def test_stream_retries_on_rate_limit(self):
        """stream() retries on 429 and succeeds on second attempt."""
        from openai import RateLimitError

        config = LLMConfig(base_url="http://fake", api_key="fake", model="test")
        client = OpenAIChatClient(config)

        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                )

            # Second call: return a mock stream
            async def mock_stream():
                yield MagicMock(
                    usage=MagicMock(prompt_tokens=10, completion_tokens=5),
                    choices=None,
                )
                yield MagicMock(
                    usage=None,
                    choices=[
                        MagicMock(
                            delta=MagicMock(content="hello", tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                )

            return mock_stream()

        # Mock the AsyncOpenAI client
        mock_oai = MagicMock()
        mock_oai.chat.completions.create = mock_create
        client._client = mock_oai

        # Patch sleep to avoid real delays
        with patch("microagent.llm.client.asyncio.sleep", new_callable=AsyncMock):
            events = []
            async for event in client.stream(
                system="sys", messages=(), tools=None
            ):
                events.append(event)

        assert call_count == 2  # first failed, second succeeded

    async def test_stream_max_retries_then_fail(self):
        """stream() retries 3 times then raises the original error."""
        from openai import RateLimitError

        config = LLMConfig(base_url="http://fake", api_key="fake", model="test")
        client = OpenAIChatClient(config)

        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            raise RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )

        mock_oai = MagicMock()
        mock_oai.chat.completions.create = mock_create
        client._client = mock_oai

        with patch("microagent.llm.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RateLimitError):
                async for _ in client.stream(
                    system="sys", messages=(), tools=None
                ):
                    pass

        # 1 initial + 3 retries = 4 total attempts
        assert call_count == 4

    async def test_stream_no_retry_on_auth_error(self):
        """stream() does NOT retry on 401 auth error."""
        from openai import AuthenticationError

        config = LLMConfig(base_url="http://fake", api_key="fake", model="test")
        client = OpenAIChatClient(config)

        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            raise AuthenticationError(
                message="bad key",
                response=MagicMock(status_code=401, headers={}),
                body=None,
            )

        mock_oai = MagicMock()
        mock_oai.chat.completions.create = mock_create
        client._client = mock_oai

        with pytest.raises(AuthenticationError):
            async for _ in client.stream(
                system="sys", messages=(), tools=None
            ):
                pass

        # Only 1 attempt — no retry
        assert call_count == 1

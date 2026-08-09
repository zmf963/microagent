"""CredentialPool — API key rotation on failure.

When one API key hits rate limit or quota exhaustion, the pool
automatically rotates to the next key. When all keys are exhausted,
it resets and reuses the first key (best-effort).
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import LLMConfig


@dataclass
class CredentialPool:
    """Rotates through multiple LLMConfig credentials on failure.

    Usage:
        pool = CredentialPool(credentials=(
            LLMConfig(base_url="...", api_key="key1", model="m"),
            LLMConfig(base_url="...", api_key="key2", model="m"),
        ))
        client = OpenAIChatClient(pool.current)
        try:
            response = await client.chat(...)
        except RateLimitError:
            pool.mark_failed()
            client = OpenAIChatClient(pool.current)  # retry with next key
    """

    credentials: tuple[LLMConfig, ...]
    _index: int = 0
    _failed: int = 0

    def __post_init__(self):
        if not self.credentials:
            raise ValueError("credential pool requires at least one LLMConfig")

    @property
    def current(self) -> LLMConfig:
        return self.credentials[self._index]

    def mark_ok(self) -> None:
        """Reset the failure counter after a successful call.

        Without this, N keys accumulate N failures across arbitrary time
        spans (with many successes in between) and then reset-jump to
        index 0 — typically the dead first key.
        """
        self._failed = 0

    def next(self) -> LLMConfig:
        """Rotate to next credential and return it."""
        self._index = (self._index + 1) % len(self.credentials)
        return self.current

    def mark_failed(self) -> None:
        """Mark current credential as failed, rotate to next.

        After rotating through all credentials, resets the failure
        counter and reuses the first one (best-effort).
        """
        self._failed += 1
        if self._failed >= len(self.credentials):
            # All credentials exhausted — reset and try the first again
            self._failed = 0
            self._index = 0
            return
        self.next()

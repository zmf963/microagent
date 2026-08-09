"""Tests for CredentialPool — API key rotation on failure."""

import pytest

from microagent.llm.client import LLMConfig
from microagent.llm.pool import CredentialPool


class TestCredentialPool:
    def test_single_key(self):
        """Single key pool works normally."""
        cfg = LLMConfig(base_url="http://x/v1", api_key="k1", model="m")
        pool = CredentialPool(credentials=(cfg,))
        assert pool.current == cfg
        assert pool.next() == cfg  # same key, no rotation needed

    def test_multiple_keys_rotate(self):
        """Multiple keys rotate on next()."""
        cfg1 = LLMConfig(base_url="http://x/v1", api_key="k1", model="m")
        cfg2 = LLMConfig(base_url="http://x/v1", api_key="k2", model="m")
        cfg3 = LLMConfig(base_url="http://x/v1", api_key="k3", model="m")
        pool = CredentialPool(credentials=(cfg1, cfg2, cfg3))

        assert pool.current == cfg1
        assert pool.next() == cfg2
        assert pool.next() == cfg3
        assert pool.next() == cfg1  # wrap around

    def test_empty_pool_raises(self):
        """Empty pool raises ValueError."""
        with pytest.raises(ValueError):
            CredentialPool(credentials=())

    def test_rotate_on_error(self):
        """After reporting an error, next key is used."""
        cfg1 = LLMConfig(base_url="http://x/v1", api_key="bad", model="m")
        cfg2 = LLMConfig(base_url="http://x/v1", api_key="good", model="m")
        pool = CredentialPool(credentials=(cfg1, cfg2))

        assert pool.current == cfg1
        pool.mark_failed()  # key1 is bad
        assert pool.current == cfg2  # auto-rotated

    def test_all_failed_resets(self):
        """When all keys fail, pool resets and reuses first key."""
        cfg1 = LLMConfig(base_url="http://x/v1", api_key="k1", model="m")
        cfg2 = LLMConfig(base_url="http://x/v1", api_key="k2", model="m")
        pool = CredentialPool(credentials=(cfg1, cfg2))

        pool.mark_failed()
        pool.mark_failed()
        # All failed → reset
        assert pool.current == cfg1


class TestMarkOk:
    def test_mark_ok_resets_failure_counter(self):
        """Successes reset _failed — otherwise N keys accumulate N failures
        across days (with many successes between) and reset-jump to the
        dead first key."""
        cfg1 = LLMConfig(base_url="http://x/v1", api_key="k1", model="m")
        cfg2 = LLMConfig(base_url="http://x/v1", api_key="k2", model="m")
        pool = CredentialPool(credentials=(cfg1, cfg2))
        pool.mark_failed()  # rotated to k2, _failed=1
        pool.mark_ok()
        assert pool._failed == 0
        # Another failure rotates again instead of triggering the reset
        pool.mark_failed()
        assert pool._failed == 1

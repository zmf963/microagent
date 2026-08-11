"""Tests for the pricing module (models.dev cache + local overrides).

Covers: cache loading, exact/suffix/prefix lookup, local-override
precedence (tx-d4f and friends), fallback behavior, and estimate_cost.
The refresh() network path is mocked since the test environment has no
reliable models.dev connectivity.
"""

import json
import pytest

from microagent.llm import pricing


class TestCacheLoading:
    def test_cache_loads_from_seed_file(self):
        pricing._cache_loaded = False
        pricing._cache.clear()
        pricing._load_cache()
        assert len(pricing._cache) > 100, "seed cache should have 364 models"
        # A few representative models must be present
        assert "openai/gpt-4o" in pricing._cache or any(
            k.endswith("/gpt-4o") for k in pricing._cache
        )

    def test_load_cache_is_idempotent(self):
        pricing._load_cache()
        n1 = len(pricing._cache)
        pricing._load_cache()
        assert len(pricing._cache) == n1


class TestPricingLookup:
    def test_known_model_exact_id(self):
        # openai/gpt-4o is $2.50/$10.00 per 1M
        inp, out = pricing.get_pricing("openai/gpt-4o")
        assert inp == pytest.approx(2.5, abs=0.01)
        assert out == pytest.approx(10.0, abs=0.01)

    def test_known_model_bare_id_suffix_match(self):
        # Bare "gpt-4o" should resolve via the "/gpt-4o" suffix
        inp, out = pricing.get_pricing("gpt-4o")
        assert inp == pytest.approx(2.5, abs=0.01)

    def test_unknown_model_falls_back(self):
        # Unknown model must NOT return 0.0 — that would silently break
        # Budget tracking. Conservative $0.50/1M fallback.
        inp, out = pricing.get_pricing("totally-fake-model-xyz-12345")
        assert inp > 0
        assert out > 0
        assert inp == pytest.approx(0.50)
        assert out == pytest.approx(0.50)


class TestGatewayAliases:
    """Gateway aliases (tx-d4f etc.) route to real paid upstream models
    through the 9router gateway — they are NOT free. Each must resolve to
    its canonical models.dev model so pricing tracks the real cost.

    Verified against the gateway's own /model response:
      tx-d4f → deepseek-v4-flash  ($0.126/$0.252 per 1M, 1M ctx)
      oc-d4f → deepseek-v4-flash
      tx-d4p → deepseek-v4-pro    ($0.435/$0.87 per 1M, 1M ctx)
    """

    def test_tx_d4f_pricing_matches_flash(self):
        # tx-d4f routes to deepseek-v4-flash — same price as the canonical
        inp, out = pricing.get_pricing("tx-d4f")
        canonical = pricing.get_pricing("deepseek/deepseek-v4-flash")
        assert (inp, out) == canonical
        assert inp > 0 and out > 0  # NOT free

    def test_oc_d4f_pricing_matches_flash(self):
        assert pricing.get_pricing("oc-d4f") == pricing.get_pricing("deepseek/deepseek-v4-flash")

    def test_tx_d4p_pricing_matches_pro(self):
        # tx-d4p routes to deepseek-v4-PRO (not flash) — different price
        assert pricing.get_pricing("tx-d4p") == pricing.get_pricing("deepseek/deepseek-v4-pro")

    def test_tx_d4f_context_is_1m(self):
        # deepseek-v4-flash has a 1M context window (was wrongly 200K)
        assert pricing.get_context_window("tx-d4f") == 1_048_576


class TestContextWindow:
    def test_known_model_context(self):
        # gpt-4o is 128K
        ctx = pricing.get_context_window("openai/gpt-4o")
        assert ctx == 128_000

    def test_unknown_model_fallback(self):
        assert pricing.get_context_window("fake-model-xyz") == 128_000

    def test_claude_opus_has_large_context(self):
        ctx = pricing.get_context_window("anthropic/claude-opus-4.7-fast")
        assert ctx >= 200_000


class TestEstimateCost:
    def test_zero_tokens_zero_cost(self):
        assert pricing.estimate_cost("openai/gpt-4o", 0, 0) == 0.0

    def test_simple_calculation(self):
        # gpt-4o: $2.50/1M input, $10.00/1M output
        # 1M input + 1M output = $12.50
        cost = pricing.estimate_cost("openai/gpt-4o", 1_000_000, 1_000_000)
        assert cost == pytest.approx(12.50, abs=0.01)

    def test_d4f_aliases_have_real_cost(self):
        # NOT free — routes to paid upstream models
        cost = pricing.estimate_cost("tx-d4f", 100_000, 50_000)
        assert cost > 0


class TestRefresh:
    def test_refresh_handles_network_failure_gracefully(self, monkeypatch):
        """If models.dev is unreachable, refresh() must keep the existing
        cache (not wipe it) and return its current size."""
        pricing._load_cache()
        before = len(pricing._cache)
        assert before > 0

        # Force every URL fetch to fail
        def _fail(*args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr(pricing.urllib.request, "urlopen", _fail)
        result = pricing.refresh()
        # Cache is intact, not wiped
        assert len(pricing._cache) == before
        assert result == before

    def test_refresh_updates_cache_on_success(self, monkeypatch, tmp_path):
        """A successful fetch replaces the cache with the new data."""
        fake_payload = {
            "data": [
                {
                    "id": "fake/fakemodel",
                    "name": "Fake Model",
                    "context_length": 999_999,
                    "pricing": {"prompt": "0.001", "completion": "0.002"},
                }
            ]
        }

        class _FakeResp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return json.dumps(self._data).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def _fake_urlopen(req, timeout=None):
            return _FakeResp(fake_payload)

        # Point the cache file at a temp location so we don't overwrite the seed
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(pricing.urllib.request, "urlopen", _fake_urlopen)

        try:
            result = pricing.refresh()
            assert result == 1
            assert "fake/fakemodel" in pricing._cache
            inp, out = pricing.get_pricing("fake/fakemodel")
            # 0.001/token = $1000/1M
            assert inp == pytest.approx(1000.0)
            assert out == pytest.approx(2000.0)
            assert pricing.get_context_window("fake/fakemodel") == 999_999
        finally:
            # refresh() replaced the module-level _cache with the fake data
            # and set _cache_loaded=True. Without resetting, later tests in
            # the session inherit the polluted cache (e.g. test_v05_features
            # resolves tx-d4f → deepseek-v4-flash which is no longer present).
            pricing._cache.clear()
            pricing._cache_loaded = False


class TestPricingMorePaths:
    def test_longest_prefix_match(self):
        """A dated/suffixed model resolves to its base prefix's pricing."""
        from microagent.llm.pricing import get_pricing
        # openai/gpt-4o-2024-08-06 → openai/gpt-4o via prefix
        p = get_pricing("openai/gpt-4o-2024-08-06")
        assert p[0] > 0

    def test_bare_id_suffix_match(self):
        from microagent.llm.pricing import get_pricing
        # 'gpt-4o' matches 'openai/gpt-4o'
        assert get_pricing("gpt-4o") == get_pricing("openai/gpt-4o")

    def test_bare_dated_id_prefix_match(self):
        """A bare dated id (no provider/) resolves via tail-prefix match."""
        from microagent.llm.pricing import get_pricing
        # 'gpt-4o-2024-08-06' should match 'openai/gpt-4o' via the
        # bare-tail-prefix path, not fall through to fallback pricing.
        assert get_pricing("gpt-4o-2024-08-06") == get_pricing("openai/gpt-4o")

    def test_refresh_failure_keeps_cache(self, monkeypatch, tmp_path):
        """refresh() with all fetches failing keeps existing cache."""
        from microagent.llm import pricing
        pricing._load_cache()
        before = len(pricing._cache)
        def _fail(*a, **k): raise OSError("net down")
        monkeypatch.setattr(pricing.urllib.request, "urlopen", _fail)
        n = pricing.refresh()
        assert n == before
        assert len(pricing._cache) == before

    def test_alias_to_canonical(self):
        from microagent.llm.pricing import get_pricing
        # tx-d4f → deepseek/deepseek-v4-flash (paid, not $0)
        p = get_pricing("tx-d4f")
        assert p[0] > 0 and p[1] > 0

    def test_estimate_cost_huge_numbers(self):
        from microagent.llm.pricing import estimate_cost
        cost = estimate_cost("openai/gpt-4o", 10**12, 10**12)
        assert cost > 0

    def test_get_context_window_matches_prefix(self):
        from microagent.llm.pricing import get_context_window
        assert get_context_window("openai/gpt-4o-2024-08-06") == 128_000

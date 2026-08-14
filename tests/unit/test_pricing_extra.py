"""Extra coverage for llm/pricing.py: cache-load failure branch, unknown
model fallbacks, refresh success with junk rows, and refresh persist failure."""

import json

import pytest

from microagent.llm import pricing


class TestCacheLoadFailure:
    def test_load_cache_failure_retries(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "missing.json")
        pricing._cache_loaded = False
        pricing._cache.clear()
        pricing._load_cache()
        assert pricing._cache_loaded is False
        assert len(pricing._cache) == 0
        # A retry after the file appears succeeds.
        (tmp_path / "missing.json").write_text(
            json.dumps({"models": {"fake/model": {"input_per_1m": 1.0, "output_per_1m": 2.0}}})
        )
        pricing._load_cache()
        assert pricing._cache_loaded is True
        assert "fake/model" in pricing._cache
        # Restore seed state for other tests.
        pricing._cache_loaded = False
        pricing._cache.clear()

    def test_load_cache_corrupt_json(self, monkeypatch, tmp_path):
        (tmp_path / "bad.json").write_text("{not json")
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "bad.json")
        pricing._cache_loaded = False
        pricing._cache.clear()
        pricing._load_cache()
        assert pricing._cache_loaded is False
        pricing._cache_loaded = False
        pricing._cache.clear()

    def test_load_cache_flat_models_payload(self, monkeypatch, tmp_path):
        (tmp_path / "flat.json").write_text(
            json.dumps({"models": {"a/b": {"input_per_1m": 3.0, "output_per_1m": 4.0}}})
        )
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "flat.json")
        pricing._cache_loaded = False
        pricing._cache.clear()
        pricing._load_cache()
        assert pricing._cache.get("a/b", {}).get("input_per_1m") == 3.0
        pricing._cache_loaded = False
        pricing._cache.clear()

    def test_load_cache_list_payload(self, monkeypatch, tmp_path):
        """A list-shaped payload (models not a dict) still marks loaded."""
        (tmp_path / "list.json").write_text(json.dumps([{"id": "x/y"}]))
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "list.json")
        pricing._cache_loaded = False
        pricing._cache.clear()
        pricing._load_cache()
        assert pricing._cache_loaded is True
        pricing._cache_loaded = False
        pricing._cache.clear()


class TestLookupBranches:
    def test_empty_model_returns_none(self):
        pricing._load_cache()
        assert pricing._lookup("") is None
        assert pricing._lookup("   ") is None

    def test_exact_match_original_case(self):
        pricing._load_cache()
        assert pricing._lookup("openai/gpt-4o") is not None

    def test_bare_dated_id_prefix_match(self):
        pricing._load_cache()
        entry = pricing._lookup("gpt-4o-2024-08-06")
        base = pricing._lookup("openai/gpt-4o")
        assert entry is not None
        assert base is not None
        assert entry["input_per_1m"] == base["input_per_1m"]

    def test_multi_candidate_suffix_prefers_shortest(self):
        pricing._load_cache()
        fake = {
            "aaaa/gpt-4o-zzz": {"input_per_1m": 9.0, "output_per_1m": 9.0},
            "bb/gpt-4o-zzz": {"input_per_1m": 7.0, "output_per_1m": 7.0},
        }
        saved = {k: pricing._cache.get(k) for k in fake}
        try:
            pricing._cache.update(fake)
            entry = pricing._lookup("gpt-4o-zzz")
            assert entry is not None
            assert entry["input_per_1m"] == 7.0  # shortest key wins
        finally:
            for k in fake:
                prior = saved.get(k)
                if prior is not None:
                    pricing._cache[k] = prior  # type: ignore[assignment]
                else:
                    pricing._cache.pop(k, None)

    def test_provider_prefix_match(self):
        """'provider/model-2024-01' matches the longest cache key prefix."""
        pricing._load_cache()
        entry = pricing._lookup("openai/gpt-4o-2024-08-06-extra-revision")
        base = pricing._lookup("openai/gpt-4o-2024-08-06")
        assert entry is not None
        assert base is not None
        assert entry["input_per_1m"] == base["input_per_1m"]

    def test_bare_dated_id_matches_via_tail_prefix(self):
        pricing._load_cache()
        entry = pricing._lookup("gpt-4o-2024-08-06-extra-revision")
        base = pricing._lookup("openai/gpt-4o")
        assert entry is not None
        assert base is not None
        assert entry["input_per_1m"] == base["input_per_1m"]

    def test_no_match_returns_none(self):
        pricing._load_cache()
        assert pricing._lookup("zzz-totally-absent-qqq") is None


class TestFallbacks:
    def test_get_pricing_empty_model(self):
        assert pricing.get_pricing("") == (0.50, 0.50)

    def test_get_pricing_unknown_model(self):
        inp, out = pricing.get_pricing("zzz-unknown-model-qqq")
        assert inp == pytest.approx(0.50)
        assert out == pytest.approx(0.50)

    def test_get_context_window_empty_model(self):
        assert pricing.get_context_window("") == 128_000

    def test_get_context_window_unknown_model(self):
        assert pricing.get_context_window("zzz-unknown-model-qqq") == 128_000

    def test_estimate_cost_unknown_model(self):
        cost = pricing.estimate_cost("zzz-unknown-model-qqq", 1_000_000, 1_000_000)
        assert cost == pytest.approx(1.0)

    def test_estimate_cost_negative_tokens_clamped(self):
        cost = pricing.estimate_cost("openai/gpt-4o", -500, -100)
        assert cost == 0.0

    def test_alias_missing_from_cache_falls_back(self, monkeypatch):
        pricing._load_cache()
        monkeypatch.setitem(pricing._cache, "deepseek/deepseek-v4-pro", None)
        assert pricing.get_pricing("tx-d4p") == (0.50, 0.50)
        monkeypatch.setitem(pricing._cache, "deepseek/deepseek-v4-pro", None)
        assert pricing.get_context_window("tx-d4p") == 128_000

    def test_alias_entry_bad_price_values_falls_back(self, monkeypatch):
        pricing._load_cache()
        monkeypatch.setitem(
            pricing._cache,
            "deepseek/deepseek-v4-flash",
            {"input_per_1m": "junk", "output_per_1m": "junk"},
        )
        assert pricing.get_pricing("tx-d4f") == (0.50, 0.50)
        assert pricing.get_context_window("tx-d4f") == 128_000

    def test_get_context_window_known_via_alias(self):
        pricing._load_cache()
        entry = pricing._lookup("deepseek/deepseek-v4-flash")
        assert entry is not None
        assert pricing.get_context_window("tx-d4f") == entry["context_length"]

    def test_entry_with_bad_price_values_falls_back(self, monkeypatch):
        pricing._load_cache()
        monkeypatch.setitem(
            pricing._cache,
            "weird/badvalues",
            {"input_per_1m": "not-a-number", "output_per_1m": "nope"},
        )
        assert pricing.get_pricing("weird/badvalues") == (0.50, 0.50)

    def test_context_window_non_int_or_zero(self, monkeypatch):
        pricing._load_cache()
        monkeypatch.setitem(
            pricing._cache,
            "weird/noctx",
            {"input_per_1m": 1.0, "output_per_1m": 1.0, "context_length": "big"},
        )
        assert pricing.get_context_window("weird/noctx") == 128_000
        monkeypatch.setitem(
            pricing._cache,
            "weird/zerocontext",
            {"input_per_1m": 1.0, "output_per_1m": 1.0, "context_length": 0},
        )
        assert pricing.get_context_window("weird/zerocontext") == 128_000


class TestRefreshMore:
    def _fake_urlopen(self, payload):
        def _f(req, timeout=None):
            return _Resp(payload)
        return _f

    def test_refresh_skips_rows_without_id_or_pricing(self, monkeypatch, tmp_path):
        payload = {
            "data": [
                {"name": "no id", "pricing": {"prompt": "1", "completion": "1"}},
                {"id": "fake/no-pricing"},
                {"id": "fake/bad-pricing", "pricing": {"prompt": "nope", "completion": "x"}},
                {
                    "id": "fake/good",
                    "name": "Good Model",
                    "context_length": 200_000,
                    "pricing": {"prompt": "0.001", "completion": "0.002"},
                },
                {
                    "id": "fake/zero-ctx",
                    "context_length": 0,
                    "pricing": {"prompt": "0.001", "completion": "0.001"},
                },
            ]
        }
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(
            pricing.urllib.request, "urlopen", self._fake_urlopen(payload)
        )
        try:
            result = pricing.refresh()
            assert result == 3
            assert "fake/good" in pricing._cache
            assert pricing._cache["fake/good"]["context_length"] == 200_000
            assert pricing._cache["fake/zero-ctx"]["context_length"] is None
            assert "no id" not in pricing._cache
            assert "fake/bad-pricing" not in pricing._cache
            assert pricing._cache["fake/no-pricing"]["input_per_1m"] == 0.0
            # Persisted payload wraps models in {"models": ...}
            saved = json.loads((tmp_path / "cache.json").read_text())
            assert "fake/good" in saved["models"]
        finally:
            pricing._cache.clear()
            pricing._cache_loaded = False

    def test_refresh_persist_failure_keeps_new_cache(self, monkeypatch, tmp_path):
        payload = {
            "data": [
                {
                    "id": "fake/keep",
                    "pricing": {"prompt": "0.001", "completion": "0.001"},
                }
            ]
        }
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(
            pricing.urllib.request, "urlopen", self._fake_urlopen(payload)
        )

        import os as _os

        def _bad_replace(src, dst):
            raise OSError("no space left")

        monkeypatch.setattr(_os, "replace", _bad_replace)
        try:
            result = pricing.refresh()
            assert result == 1
            assert "fake/keep" in pricing._cache
        finally:
            pricing._cache.clear()
            pricing._cache_loaded = False

    def test_refresh_success_via_mirror(self, monkeypatch, tmp_path):
        """Primary URL fails; the mirror succeeds."""
        payload = {
            "data": [
                {
                    "id": "fake/mirror",
                    "pricing": {"prompt": "0.001", "completion": "0.002"},
                }
            ]
        }
        calls = []

        def _flaky(req, timeout=None):
            calls.append(req.full_url)
            if req.full_url == pricing._REMOTE_URL:
                raise OSError("primary down")
            return _Resp(payload)

        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(pricing.urllib.request, "urlopen", _flaky)
        try:
            result = pricing.refresh()
            assert result == 1
            assert len(calls) == 2
            assert "fake/mirror" in pricing._cache
        finally:
            pricing._cache.clear()
            pricing._cache_loaded = False

    def test_refresh_payload_flat_list(self, monkeypatch, tmp_path):
        """A list payload (not wrapped in {"data": ...}) also works."""
        payload = [
            {
                "id": "fake/flat",
                "pricing": {"prompt": "0.001", "completion": "0.001"},
            }
        ]
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(
            pricing.urllib.request, "urlopen", self._fake_urlopen(payload)
        )
        try:
            result = pricing.refresh()
            assert result == 1
            assert "fake/flat" in pricing._cache
        finally:
            pricing._cache.clear()
            pricing._cache_loaded = False

    def test_refresh_mkstemp_failure_keeps_new_cache(self, monkeypatch, tmp_path):
        """If temp file creation fails (OSError), the in-memory cache is
        still replaced — only persistence is skipped."""
        payload = {
            "data": [
                {
                    "id": "fake/nopersist",
                    "pricing": {"prompt": "0.001", "completion": "0.001"},
                }
            ]
        }
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(
            pricing.urllib.request, "urlopen", self._fake_urlopen(payload)
        )

        import tempfile as _tf

        def _bad_mkstemp(**kwargs):
            raise OSError("readonly dir")

        monkeypatch.setattr(_tf, "mkstemp", _bad_mkstemp)
        try:
            result = pricing.refresh()
            assert result == 1
            assert "fake/nopersist" in pricing._cache
        finally:
            pricing._cache.clear()
            pricing._cache_loaded = False

    def test_refresh_write_failure_unlinks_temp(self, monkeypatch, tmp_path):
        payload = {
            "data": [
                {
                    "id": "fake/unlink",
                    "pricing": {"prompt": "0.001", "completion": "0.001"},
                }
            ]
        }
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(
            pricing.urllib.request, "urlopen", self._fake_urlopen(payload)
        )

        import os as _os

        unlinked = []

        class _BadFile:
            def write(self, data):
                raise OSError("disk full")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def _bad_fdopen(fd, mode):
            return _BadFile()

        monkeypatch.setattr(_os, "fdopen", _bad_fdopen)
        real_unlink = _os.unlink

        def _tracking_unlink(path):
            unlinked.append(path)
            real_unlink(path)

        monkeypatch.setattr(_os, "unlink", _tracking_unlink)
        try:
            result = pricing.refresh()
            assert result == 1
            assert "fake/unlink" in pricing._cache
            assert unlinked  # temp file cleaned up
        finally:
            pricing._cache.clear()
            pricing._cache_loaded = False

    def test_refresh_unlink_failure_swallowed(self, monkeypatch, tmp_path):
        """If temp cleanup's unlink itself fails (OSError), refresh still
        returns the new cache count."""
        payload = {
            "data": [
                {
                    "id": "fake/unlinkfail",
                    "pricing": {"prompt": "0.001", "completion": "0.001"},
                }
            ]
        }
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(
            pricing.urllib.request, "urlopen", self._fake_urlopen(payload)
        )

        import os as _os

        class _BadFile:
            def write(self, data):
                raise OSError("disk full")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr(_os, "fdopen", lambda fd, mode: _BadFile())

        def _bad_unlink(path):
            raise OSError("unlink broken")

        monkeypatch.setattr(_os, "unlink", _bad_unlink)
        try:
            result = pricing.refresh()
            assert result == 1
            assert "fake/unlinkfail" in pricing._cache
        finally:
            pricing._cache.clear()
            pricing._cache_loaded = False


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

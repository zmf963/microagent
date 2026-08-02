"""Final coverage push: bash kill-group fallback, git timeout, grep edge
cases, search single-CJK, pricing internals."""

import pytest


class TestBashKillGroup:
    def test_kill_group_falls_back_to_kill(self):
        """_kill_proc_group falls back to proc.kill() when killpg fails."""
        from microagent.tools.builtins.bash import _kill_proc_group

        class _Proc:
            killed = False
            pid = 999999

            def kill(self):
                _Proc.killed = True

        _Proc.killed = False
        # getpgid will fail (no such process) → fallback to proc.kill()
        _kill_proc_group(_Proc())
        assert _Proc.killed is True


class TestGitTimeout:
    @pytest.mark.asyncio
    async def test_git_timeout(self, monkeypatch):
        """A hanging git command times out after 60s and kills the proc."""
        import asyncio
        from microagent.tools.builtins import git as git_mod

        class _Proc:
            killed = False
            returncode = -9

            async def communicate(self):
                raise asyncio.TimeoutError()

            def kill(self):
                _Proc.killed = True

            async def wait(self):
                pass

        async def _fake_exec(*a, **k):
            return _Proc()

        monkeypatch.setattr(git_mod.asyncio, "create_subprocess_exec", _fake_exec)
        # Patch wait_for to actually raise TimeoutError from communicate
        orig_wait_for = git_mod.asyncio.wait_for

        async def _wait_for(awaitable, timeout=None):
            return await awaitable  # let communicate's TimeoutError propagate

        monkeypatch.setattr(git_mod.asyncio, "wait_for", _wait_for)
        r = await git_mod.git.fn(subcommand="status", repo_path=".")
        assert r.is_error
        assert "timed out" in r.content
        assert _Proc.killed


class TestGrepEdgeCases:
    @pytest.mark.asyncio
    async def test_skips_oversized_file(self, tmp_path):
        from microagent.tools.builtins.grep import grep, _MAX_FILE_BYTES
        f = tmp_path / "big.txt"
        f.write_bytes(b"needle\n" + b"x" * (_MAX_FILE_BYTES + 1))
        r = await grep.fn(pattern="needle", path=str(tmp_path))
        # oversized file skipped
        assert "(no matches)" in r.content

    @pytest.mark.asyncio
    async def test_skips_binary_file(self, tmp_path):
        from microagent.tools.builtins.grep import grep
        f = tmp_path / "bin.dat"
        f.write_bytes(b"needle\x00\x01")
        r = await grep.fn(pattern="needle", path=str(tmp_path))
        assert "(no matches)" in r.content

    @pytest.mark.asyncio
    async def test_truncates_results(self, tmp_path):
        from microagent.tools.builtins.grep import grep
        d = tmp_path / "f.txt"
        d.write_text("\n".join(f"match{i}" for i in range(20)))
        r = await grep.fn(pattern="match", path=str(tmp_path), max_results=5)
        assert "truncated at 5 results" in r.content

    @pytest.mark.asyncio
    async def test_path_not_found(self):
        from microagent.tools.builtins.grep import grep
        r = await grep.fn(pattern="x", path="/nonexistent-xyz")
        assert r.is_error
        assert "path not found" in r.content

    @pytest.mark.asyncio
    async def test_invalid_regex(self, tmp_path):
        from microagent.tools.builtins.grep import grep
        r = await grep.fn(pattern="[invalid", path=str(tmp_path))
        assert r.is_error
        assert "invalid regex" in r.content


class TestSearchSingleCJK:
    def test_single_cjk_char(self):
        from microagent.session.search import _build_fts_query
        q = _build_fts_query("中")
        assert "中" in q  # single CJK char kept as-is

    def test_mixed_with_single_cjk(self):
        from microagent.session.search import _build_fts_query
        q = _build_fts_query("py 中")
        assert "py" in q or '"py"' in q


class TestPricingInternals:
    def test_refresh_writes_new_cache(self, monkeypatch, tmp_path):
        import json
        from microagent.llm import pricing

        payload = {"data": [{"id": "x/y", "name": "Y", "context_length": 1000,
                             "pricing": {"prompt": "0.001", "completion": "0.002"}}]}

        class _Resp:
            def read(self):
                return json.dumps(payload).encode()

        class _Ctx:
            def __enter__(self): return _Resp()
            def __exit__(self, *a): pass

        def _urlopen(req, timeout=None):
            return _Ctx()

        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(pricing.urllib.request, "urlopen", _urlopen)
        n = pricing.refresh()
        assert n == 1
        assert "x/y" in pricing._cache
        # cache file persisted
        assert (tmp_path / "cache.json").exists()

    def test_load_cache_retries_after_failure(self, monkeypatch, tmp_path):
        from microagent.llm import pricing
        pricing._cache_loaded = False
        pricing._cache.clear()
        monkeypatch.setattr(pricing, "_CACHE_FILE", tmp_path / "missing.json")
        pricing._load_cache()
        # failure does NOT set _cache_loaded → next call retries
        assert pricing._cache_loaded is False
        # Now write the file and retry
        import json
        (tmp_path / "missing.json").write_text(json.dumps({"models": {"a/b": {"name": "B"}}}))
        pricing._load_cache()
        assert pricing._cache_loaded is True
        assert "a/b" in pricing._cache

"""Extra coverage for skill/learner.py: _collect_material error paths,
name safety, LLM stream failures, and auxiliary-model distillation."""

import httpx
import pytest
from pathlib import Path

from microagent.llm.client import LLMConfig
from microagent.core.types import Message, TextDelta
from microagent.skill import learner


GOOD_OUTPUT = (
    "---\nname: extra-demo\ndescription: demo extra skill\n"
    "---\n\n# Extra Demo\n\nRun `make demo`.\n"
)


class _RecLLM:
    def __init__(self, output, config=None, fail=False):
        self.config = config or LLMConfig("fake", "fake-key", "fake-model")
        self._output = output
        self._fail = fail
        self.for_model_calls = []
        self.stream_calls = []

    async def stream(self, system, messages, tools):
        self.stream_calls.append(messages)
        if self._fail:
            raise RuntimeError("stream exploded")
        yield TextDelta(text=self._output, kind="content")

    def for_model(self, model):
        self.for_model_calls.append(model)
        return self


class TestCollectMaterial:
    async def test_dir_not_found(self, tmp_path):
        missing = tmp_path / "nope"
        with pytest.raises(ValueError, match="not a directory"):
            await learner._collect_material(str(missing), "dir")

    async def test_dir_truncates_oversized_file(self, tmp_path):
        d = tmp_path / "big"
        d.mkdir()
        (d / "huge.txt").write_text("x" * 300_000)
        (d / "small.txt").write_text("tiny")
        material = await learner._collect_material(str(d), "dir")
        assert "(truncated)" in material
        assert "huge.txt" in material
        assert "small.txt" in material

    async def test_dir_further_files_omitted_cap(self, tmp_path):
        d = tmp_path / "many"
        d.mkdir()
        for i in range(40):
            (d / f"f{i:02}.txt").write_text("payload " * 300)
        material = await learner._collect_material(str(d), "dir")
        assert "further files omitted" in material
        assert "f00.txt" in material
        assert "f39.txt" not in material

    async def test_dir_skips_git_and_pycache(self, tmp_path):
        d = tmp_path / "tree"
        (d / ".git").mkdir(parents=True)
        (d / "__pycache__").mkdir()
        (d / ".git" / "config").write_text("secret")
        (d / "__pycache__" / "x.pyc").write_bytes(b"\x00" * 10)
        (d / "real.txt").write_text("hello")
        material = await learner._collect_material(str(d), "dir")
        assert "real.txt" in material
        assert "secret" not in material
        assert "x.pyc" not in material

    async def test_url_non_http_scheme(self):
        with pytest.raises(ValueError, match="unsupported URL scheme"):
            await learner._collect_material("ftp://example.com/file", "url")

    async def test_url_blocked_resolution(self, monkeypatch):
        async def _blocked(func, *args):
            return f"blocked: {args[0]!r}"
        monkeypatch.setattr("asyncio.to_thread", _blocked)
        with pytest.raises(ValueError, match="SSRF"):
            await learner._collect_material("http://127.0.0.1/x", "url")

    async def test_url_fetch_success(self, monkeypatch):
        async def _ok(func, *args):
            return None
        monkeypatch.setattr("asyncio.to_thread", _ok)

        class _Resp:
            status_code = 200
            headers: dict = {}

            def raise_for_status(self):
                pass
            text = "fetched body"

        class _Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url):
                self.url = url
                return _Resp()

        captured = {}
        def _factory(**kwargs):
            captured.update(kwargs)
            return _Client(**kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", _factory)
        material = await learner._collect_material("https://example.com/doc", "url")
        assert material == "fetched body"
        assert captured["follow_redirects"] is False

    async def test_unknown_kind(self):
        with pytest.raises(ValueError, match="unknown kind"):
            await learner._collect_material("x", "telepathy")


class TestExtractName:
    def test_missing(self):
        assert learner._extract_name("no frontmatter here") is None

    def test_empty_value(self):
        assert learner._extract_name("---\nname:\n---\n") is None

    def test_found(self):
        assert learner._extract_name("---\nname: my-skill\n---\n") == "my-skill"


class TestLearnSkillErrors:
    async def test_unsafe_name_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        output = "---\nname: evil/../skill\ndescription: x\n---\n# Bad\n"
        result = await learner.learn_skill("chat", llm=_RecLLM(output))
        assert result.startswith("[error]")
        assert "safe" in result

    async def test_llm_stream_exception(self, tmp_path):
        result = await learner.learn_skill("chat", llm=_RecLLM("x", fail=True))
        assert result == "[error] LLM call failed: RuntimeError('stream exploded')"

    async def test_empty_llm_response(self, tmp_path):
        result = await learner.learn_skill("chat", llm=_RecLLM("   "))
        assert result == "[error] LLM returned an empty skill"

    async def test_empty_material(self, tmp_path):
        result = await learner.learn_skill("   ", kind="chat", llm=_RecLLM(GOOD_OUTPUT))
        assert result == "[error] source material is empty"

    async def test_collect_failure_reported(self, tmp_path):
        result = await learner.learn_skill("x", kind="telepathy", llm=_RecLLM(GOOD_OUTPUT))
        assert result.startswith("[error] failed to read source")

    async def test_already_exists_reports_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        d = tmp_path / ".microagent" / "skills" / "extra-demo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("old")
        result = await learner.learn_skill("chat", llm=_RecLLM(GOOD_OUTPUT))
        assert result.startswith("[error] failed to write skill")

    async def test_auxiliary_model_used(self, monkeypatch, tmp_path):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        config = LLMConfig("fake", "fake-key", "fake-model", auxiliary_model="aux-model")
        llm = _RecLLM(GOOD_OUTPUT, config=config)
        result = await learner.learn_skill("chat", llm=llm)
        assert "extra-demo" in result
        assert llm.for_model_calls == ["aux-model"]
        assert llm.stream_calls
        prompt = llm.stream_calls[0][0]
        assert isinstance(prompt, Message)
        assert "chat" in prompt.content

    async def test_auxiliary_for_model_failure_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        config = LLMConfig("fake", "fake-key", "fake-model", auxiliary_model="aux-model")
        llm = _RecLLM(GOOD_OUTPUT, config=config)

        def _bad_for_model(model):
            raise RuntimeError("no aux support")

        llm.for_model = _bad_for_model
        result = await learner.learn_skill("chat", llm=llm)
        assert "extra-demo" in result
        assert llm.stream_calls

    async def test_invalidate_all_failure_swallowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from microagent.skill.loader import ClaudeSkillLoader

        monkeypatch.setattr(
            ClaudeSkillLoader,
            "invalidate_all",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        result = await learner.learn_skill("chat", llm=_RecLLM(GOOD_OUTPUT))
        assert "extra-demo" in result

    async def test_dir_unreadable_file_skipped(self, tmp_path, monkeypatch):
        d = tmp_path / "unreadable"
        d.mkdir()
        (d / "good.txt").write_text("ok")

        class _BadStat:
            def is_file(self):
                return True

            def __lt__(self, other):
                return False

            def __gt__(self, other):
                return True

            @property
            def parts(self):
                return ("bad.txt",)

            @property
            def name(self):
                return "bad.txt"

            def stat(self):
                class _S:
                    st_size = 10
                return _S()

            def read_text(self, **kwargs):
                raise OSError("permission denied")

        original_rglob = Path.rglob

        def _rglob(self, pattern):
            yield from original_rglob(self, pattern)
            yield _BadStat()

        monkeypatch.setattr(Path, "rglob", _rglob)
        material = await learner._collect_material(str(d), "dir")
        assert "good.txt" in material
        assert "bad.txt" not in material


class TestForkedClientClosed:
    """Round-22 🟡: /learn with an auxiliary_model forked a new LLM client
    (own AsyncOpenAI pool) and never closed it — one leaked pool per
    learn/compression call."""

    async def test_learn_closes_forked_client(self, monkeypatch, tmp_path):
        from microagent.skill import learner as learner_mod

        closed = []

        class _Cfg:
            auxiliary_model = "cheap-model"

        class _Fork:
            config = _Cfg()

            async def stream(self, *, system, messages, tools=None):
                from microagent.core.types import TextDelta

                yield TextDelta(
                    text="---\nname: learned-thing\ndescription: x\n---\nbody",
                    kind="content",
                )

            async def close(self):
                closed.append("fork")

        class _Main(_Fork):
            def for_model(self, model):
                return _Fork()

            async def close(self):
                closed.append("main")

        async def _material(source, kind):
            return "material"

        monkeypatch.setattr(learner_mod, "_collect_material", _material)
        monkeypatch.setattr(learner_mod, "LEARN_PROMPT", "{material}")
        result = await learner_mod.learn_skill(
            "chat", llm=_Main(), skills_dir=tmp_path
        )
        assert not result.startswith("[error]"), result
        assert "fork" in closed, "forked auxiliary client was never closed"
        assert "main" not in closed, "must NOT close the shared main client"


class TestLearnUrlRedirects:
    """Round-22 🟡: follow_redirects=False + raise_for_status() raised on
    3xx (httpx raises for ALL non-2xx), so http→https redirects — the
    common case — failed every /learn url. Redirects are now followed
    manually with a per-hop SSRF re-check."""

    async def test_redirect_followed_and_hop_ssrf_enforced(self, monkeypatch):
        import httpx
        from microagent.skill.learner import _collect_material
        from microagent.tools.builtins import web_fetch

        checked_hosts = []

        def _fake_resolve(host):
            checked_hosts.append(host)
            if host == "169.254.169.254":
                return "blocked: link-local address"
            return None

        monkeypatch.setattr(web_fetch, "_resolve_and_check", _fake_resolve)
        # learner imports the symbol at call time via module attr lookup —
        # it does `from ..tools.builtins.web_fetch import _resolve_and_check`
        # inside the function, so patching the module attribute works.

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "example.com":
                return httpx.Response(301, headers={"location": "https://secure.example.org/doc"})
            if request.url.host == "secure.example.org":
                return httpx.Response(200, text="learned content")
            if request.url.host == "evil.example":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/meta"})
            raise AssertionError(f"unexpected host {request.url.host}")

        transport = httpx.MockTransport(handler)
        orig_client = httpx.AsyncClient

        def _patched_client(**kwargs):
            kwargs["transport"] = transport
            return orig_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _patched_client)

        out = await _collect_material("http://example.com/doc", "url")
        assert "learned content" in out
        assert "example.com" in checked_hosts and "secure.example.org" in checked_hosts

        with pytest.raises(ValueError, match="SSRF"):
            await _collect_material("http://evil.example/x", "url")

    async def test_redirect_loop_bounded(self, monkeypatch):
        import httpx
        from microagent.skill.learner import _collect_material
        from microagent.tools.builtins import web_fetch

        monkeypatch.setattr(web_fetch, "_resolve_and_check", lambda h: None)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": str(request.url)})

        transport = httpx.MockTransport(handler)
        orig_client = httpx.AsyncClient

        def _patched_client(**kwargs):
            kwargs["transport"] = transport
            return orig_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _patched_client)
        with pytest.raises(ValueError, match="too many redirects"):
            await _collect_material("http://loopy.example/loop", "url")

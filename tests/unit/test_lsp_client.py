"""Tests for LSPClient internals and the lsp tool's per-action branches.

No real language server is spawned: _read_loop and _send are driven with
fake stdout/stdin objects, and the tool is exercised with a fake client
injected through _get_client.
"""

import asyncio

import pytest

from microagent.tools.builtins import lsp as lsp_mod
from microagent.tools.builtins.lsp import LSPClient, _get_state


class _FakeStdin:
    def __init__(self):
        self.data = b""

    def write(self, d):
        self.data += d

    async def drain(self):
        return None


class _FakeProc:
    def __init__(self, stdin=None):
        self.stdin = stdin if stdin is not None else _FakeStdin()
        self.returncode = None

    async def wait(self):
        return 0

    def kill(self):
        self.killed = True


class _FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _frame(payload: bytes) -> bytes:
    return b"Content-Length: %d\r\n\r\n%s" % (len(payload), payload)


def _client_with_stdout(*chunks) -> LSPClient:
    client = LSPClient(("fake-server",), "file:///tmp")
    proc = _FakeProc()
    proc.stdout = _FakeStdout(chunks)
    client._proc = proc
    return client


def _pending(client, rid=1):
    fut = asyncio.get_running_loop().create_future()
    client._pending[rid] = fut
    return fut


class TestReadLoop:
    async def test_dispatches_response_to_pending_future(self):
        client = _client_with_stdout(_frame(b'{"jsonrpc":"2.0","id":1,"result":{"x":1}}'))
        fut = _pending(client)
        await client._read_loop()
        assert fut.done()
        assert fut.result() == {"x": 1}
        assert client._pending == {}

    async def test_error_response_sets_exception(self):
        client = _client_with_stdout(
            _frame(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"boom"}}')
        )
        fut = _pending(client)
        await client._read_loop()
        assert fut.done()
        with pytest.raises(RuntimeError, match="boom"):
            fut.result()

    async def test_response_for_unknown_id_ignored(self):
        client = _client_with_stdout(_frame(b'{"jsonrpc":"2.0","id":99,"result":null}'))
        fut = _pending(client)
        await client._read_loop()
        assert not fut.done()

    async def test_response_for_cancelled_future_ignored(self):
        client = _client_with_stdout(_frame(b'{"jsonrpc":"2.0","id":1,"result":null}'))
        fut = _pending(client)
        fut.cancel()
        await client._read_loop()
        assert client._pending == {}

    async def test_zero_content_length_frame_skipped(self):
        client = _client_with_stdout(
            b"Content-Length: 0\r\n\r\n",
            _frame(b'{"jsonrpc":"2.0","id":1,"result":"ok"}'),
        )
        fut = _pending(client)
        await client._read_loop()
        assert fut.result() == "ok"

    async def test_invalid_json_body_skipped(self):
        client = _client_with_stdout(
            _frame(b"not-json"),
            _frame(b'{"jsonrpc":"2.0","id":1,"result":"ok"}'),
        )
        fut = _pending(client)
        await client._read_loop()
        assert fut.result() == "ok"

    async def test_incomplete_body_waits_for_more(self):
        payload = b'{"jsonrpc":"2.0","id":1,"result":"ok"}'
        header = b"Content-Length: 100\r\n\r\n"
        client = _client_with_stdout(header + payload)
        fut = _pending(client)
        await client._read_loop()
        assert not fut.done()

    async def test_malformed_header_logs_warning_and_stops(self, caplog):
        client = _client_with_stdout(b"Content-Length: notanumber\r\n\r\n{}")
        fut = _pending(client)
        with caplog.at_level("WARNING", logger="microagent.tools.builtins.lsp"):
            await client._read_loop()
        assert "LSP read loop error" in caplog.text
        assert not fut.done()

    async def test_oversized_content_length_logs_warning_and_stops(self, caplog):
        client = _client_with_stdout(b"Content-Length: 99999999\r\n\r\n")
        with caplog.at_level("WARNING", logger="microagent.tools.builtins.lsp"):
            await client._read_loop()
        assert "LSP read loop error" in caplog.text


class TestRequestTimeout:
    async def test_timeout_cleans_pending(self, monkeypatch):
        real_wait_for = asyncio.wait_for

        async def _short_wait_for(fut, timeout=None):
            if timeout == 30.0:
                timeout = 0.05
            return await real_wait_for(fut, timeout)

        monkeypatch.setattr(lsp_mod.asyncio, "wait_for", _short_wait_for)
        client = LSPClient(("fake",), "file:///tmp")
        with pytest.raises(TimeoutError):
            await client._request("never-answered", {})
        assert client._pending == {}


class TestEnsureOpen:
    async def test_happy_path_and_caching(self, tmp_path):
        client = LSPClient(("fake",), "file:///tmp")
        client._proc = _FakeProc()
        f = tmp_path / "x.py"
        f.write_text("print(1)\n")
        uri = await client.ensure_open(str(f))
        assert uri == f.resolve().as_uri()
        assert b"textDocument/didOpen" in client._proc.stdin.data
        before = len(client._proc.stdin.data)
        uri2 = await client.ensure_open(str(f))
        assert uri2 == uri
        assert len(client._proc.stdin.data) == before

    async def test_missing_file_raises(self, tmp_path):
        client = LSPClient(("fake",), "file:///tmp")
        with pytest.raises(FileNotFoundError):
            await client.ensure_open(str(tmp_path / "missing.py"))


class TestSendAndNotify:
    async def test_notify_sends_framed_message(self):
        client = LSPClient(("fake",), "file:///tmp")
        client._proc = _FakeProc()
        await client._notify("exit", {})
        assert b"Content-Length:" in client._proc.stdin.data
        assert b'"method": "exit"' in client._proc.stdin.data

    async def test_send_without_proc_is_noop(self):
        client = LSPClient(("fake",), "file:///tmp")
        await client._send("x")


class TestDrainStderr:
    async def test_eof_breaks(self):
        client = LSPClient(("fake",), "file:///tmp")
        proc = _FakeProc()

        class _EOFStderr:
            async def read(self, n):
                return b""

        proc.stderr = _EOFStderr()
        client._proc = proc
        await client._drain_stderr()

    async def test_read_error_logged(self, caplog):
        client = LSPClient(("fake",), "file:///tmp")
        proc = _FakeProc()

        class _BadStderr:
            async def read(self, n):
                raise OSError("gone")

        proc.stderr = _BadStderr()
        client._proc = proc
        with caplog.at_level("DEBUG", logger="microagent.tools.builtins.lsp"):
            await client._drain_stderr()
        assert "stderr drain ended" in caplog.text


class TestStart:
    async def test_start_noop_when_proc_already_set(self):
        client = LSPClient(("fake",), "file:///tmp")
        client._proc = object()
        await client.start()
        assert client._initialized is False


class TestShutdown:
    async def test_shutdown_without_proc_is_noop(self):
        client = LSPClient(("fake",), "file:///tmp")
        await client.shutdown()

    async def test_unresponsive_shutdown_request_falls_through(self, monkeypatch):
        real_wait_for = asyncio.wait_for

        async def _short_wait_for(fut, timeout=None):
            if timeout == 2.0:
                timeout = 0.05
            return await real_wait_for(fut, timeout)

        monkeypatch.setattr(lsp_mod.asyncio, "wait_for", _short_wait_for)
        client = LSPClient(("fake",), "file:///tmp")
        client._proc = _FakeProc()
        stdin = client._proc.stdin
        await client.shutdown()
        assert client._proc is None
        assert b'"method": "exit"' in stdin.data
        assert client._pending == {}


class TestFormatLocations:
    def test_none_returns_empty(self):
        assert LSPClient._format_locations(None) == []

    def test_single_dict(self):
        out = LSPClient._format_locations(
            {"uri": "file:///a.py", "range": {"start": {"line": 4, "character": 2}}}
        )
        assert out == [
            {
                "uri": "file:///a.py",
                "range": {"start": {"line": 4, "character": 2}},
                "line": 5,
                "col": 3,
            }
        ]

    def test_list_filters_non_dicts(self):
        out = LSPClient._format_locations([{"uri": "u", "range": {}}, "junk", None])
        assert len(out) == 1
        assert out[0]["line"] == 1
        assert out[0]["col"] == 1


class _StubClient:
    def __init__(self, results):
        self._results = results

    async def _eo(self, fp):
        return "file:///x"

    async def _req(self, method, params):
        v = self._results.get(method, self._results.get("result"))
        if isinstance(v, BaseException):
            raise v
        return v


def _stub(monkeypatch, result):
    client = LSPClient(("fake",), "file:///tmp")
    stub = _StubClient({"result": result})
    monkeypatch.setattr(client, "ensure_open", stub._eo)
    monkeypatch.setattr(client, "_request", stub._req)
    return client


class TestSymbolsMethod:
    async def test_none_result(self, monkeypatch):
        client = _stub(monkeypatch, None)
        assert await client.symbols("x.py") == []

    async def test_non_list_result(self, monkeypatch):
        client = _stub(monkeypatch, {"not": "a list"})
        assert await client.symbols("x.py") == []

    async def test_filters_and_flattens(self, monkeypatch):
        result = [
            {"name": "(anonymous struct)", "kind": 23, "range": {"start": {"line": 0}}},
            {
                "name": "Cls",
                "kind": 5,
                "range": {"start": {"line": 2}},
                "children": [
                    {"name": "meth", "kind": 6, "range": {"start": {"line": 3}}},
                    {"name": "(anonymous)", "kind": 0},
                ],
            },
            {"name": "sel", "kind": 13, "selectionRange": {"start": {"line": 9}}},
        ]
        client = _stub(monkeypatch, result)
        out = await client.symbols("x.py")
        assert [s["name"] for s in out] == ["Cls", "meth", "sel"]
        assert out[0]["kind"] == "class"
        assert out[0]["line"] == 3
        assert out[0]["depth"] == 0
        assert out[1]["kind"] == "method"
        assert out[1]["line"] == 4
        assert out[1]["depth"] == 1
        assert out[2]["line"] == 10


class TestHoverMethod:
    async def test_no_hover(self, monkeypatch):
        assert await _stub(monkeypatch, None).hover("x.py", 1, 1) == "(no hover info)"

    async def test_dict_value(self, monkeypatch):
        client = _stub(monkeypatch, {"contents": {"value": "v1"}})
        assert await client.hover("x.py", 1, 1) == "v1"

    async def test_dict_without_value(self, monkeypatch):
        client = _stub(monkeypatch, {"contents": {"lang": "py"}})
        assert await client.hover("x.py", 1, 1) == "{'lang': 'py'}"

    async def test_list_contents(self, monkeypatch):
        client = _stub(monkeypatch, {"contents": ["a", {"value": "b"}, "c"]})
        assert await client.hover("x.py", 1, 1) == "a\nb\nc"

    async def test_plain_contents(self, monkeypatch):
        client = _stub(monkeypatch, {"contents": "plain"})
        assert await client.hover("x.py", 1, 1) == "plain"

    async def test_non_dict_result(self, monkeypatch):
        client = _stub(monkeypatch, "raw")
        assert await client.hover("x.py", 1, 1) == "raw"


class TestDefinitionReferences:
    async def test_definition_formats(self, monkeypatch):
        client = _stub(
            monkeypatch,
            [{"uri": "file:///a.py", "range": {"start": {"line": 0, "character": 0}}}],
        )
        out = await client.definition("x.py", 1, 1)
        assert out[0]["line"] == 1

    async def test_definition_none(self, monkeypatch):
        assert await _stub(monkeypatch, None).definition("x.py", 1, 1) == []

    async def test_references_empty(self, monkeypatch):
        assert await _stub(monkeypatch, []).references("x.py", 1, 1) == []

    async def test_references_list(self, monkeypatch):
        client = _stub(
            monkeypatch,
            [
                {"uri": "file:///a.py", "range": {"start": {"line": 0}}},
                {"uri": "file:///b.py", "range": {"start": {"line": 1}}},
            ],
        )
        out = await client.references("x.py", 1, 1)
        assert len(out) == 2
        assert out[1]["line"] == 2


class TestGetState:
    def test_creates_state_when_unset(self):
        lsp_mod._current_state.set(None)
        state = _get_state()
        assert isinstance(state, lsp_mod.LSPSessionState)
        assert _get_state() is state


class _GetClientFake:
    created = []

    def __init__(self, command, root_uri):
        self.command = command
        self.root_uri = root_uri
        self.started = False
        self._proc = _FakeProc()
        _GetClientFake.created.append(self)

    async def start(self):
        self.started = True


class TestGetClient:
    def _patch(self, monkeypatch, tmp_path):
        _GetClientFake.created = []
        f = tmp_path / "x.py"
        f.write_text("x = 1\n")
        monkeypatch.setattr(lsp_mod, "LSPClient", _GetClientFake)
        monkeypatch.setattr(lsp_mod, "_find_lsp_command", lambda lang: ("fake-server",))
        lsp_mod._current_state.set(None)
        return f

    async def test_creates_client(self, tmp_path, monkeypatch):
        f = self._patch(monkeypatch, tmp_path)
        client = await lsp_mod._get_client(str(f))
        assert client is _GetClientFake.created[0]
        assert client.started
        assert client.root_uri == tmp_path.as_uri()
        assert lsp_mod._get_state().clients["python"] is client

    async def test_evicts_client_without_proc(self, tmp_path, monkeypatch):
        f = self._patch(monkeypatch, tmp_path)
        state = lsp_mod._get_state()

        class _Dead:
            _proc = None

        state.clients["python"] = _Dead()
        client = await lsp_mod._get_client(str(f))
        assert client is _GetClientFake.created[0]
        assert state.clients["python"] is client

    async def test_evicts_client_with_exited_proc(self, tmp_path, monkeypatch):
        f = self._patch(monkeypatch, tmp_path)
        state = lsp_mod._get_state()

        class _ExitedProc:
            returncode = 1

        class _Dead:
            _proc = _ExitedProc()

        state.clients["python"] = _Dead()
        client = await lsp_mod._get_client(str(f))
        assert client is _GetClientFake.created[0]

    async def test_reuses_alive_client(self, tmp_path, monkeypatch):
        f = self._patch(monkeypatch, tmp_path)
        state = lsp_mod._get_state()
        alive = _GetClientFake(("fake-server",), tmp_path.as_uri())
        state.clients["python"] = alive
        client = await lsp_mod._get_client(str(f))
        assert client is alive
        assert alive.started is False

    async def test_none_for_unsupported_language(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("text")
        assert await lsp_mod._get_client(str(f)) is None

    async def test_none_when_no_command(self, tmp_path, monkeypatch):
        f = tmp_path / "x.py"
        f.write_text("x = 1\n")
        monkeypatch.setattr(lsp_mod, "_find_lsp_command", lambda lang: None)
        assert await lsp_mod._get_client(str(f)) is None


class _ToolFakeClient:
    def __init__(self, **results):
        self._results = results

    async def symbols(self, fp):
        return self._dispatch("symbols")

    async def definition(self, fp, line, char):
        return self._dispatch("definition")

    async def references(self, fp, line, char):
        return self._dispatch("references")

    async def hover(self, fp, line, char):
        return self._dispatch("hover")

    def _dispatch(self, name):
        v = self._results.get(name)
        if isinstance(v, BaseException):
            raise v
        return v


def _loc(uri, line):
    return {"uri": uri, "line": line, "col": 1}


class TestLSPToolActions:
    def _patch(self, monkeypatch, tmp_path, client):
        f = tmp_path / "x.py"
        f.write_text("x = 1\n")
        monkeypatch.setattr(lsp_mod, "_get_client", _FakeGetClient(client))
        return f

    async def _run(self, monkeypatch, tmp_path, client, **kwargs):
        f = self._patch(monkeypatch, tmp_path, client)
        kwargs.setdefault("filepath", str(f))
        kwargs.setdefault("action", "symbols")
        return await lsp_mod.lsp.fn(**kwargs)

    async def test_symbols_empty_result(self, tmp_path, monkeypatch):
        r = await self._run(monkeypatch, tmp_path, _ToolFakeClient(symbols=[]))
        assert not r.is_error
        assert "(no symbols found)" in r.content

    async def test_definition_found(self, tmp_path, monkeypatch):
        client = _ToolFakeClient(definition=[_loc("file:///x.py", 7)])
        r = await self._run(monkeypatch, tmp_path, client, action="definition")
        assert not r.is_error
        assert "x.py:7:1" in r.content

    async def test_definition_not_found(self, tmp_path, monkeypatch):
        client = _ToolFakeClient(definition=[])
        r = await self._run(
            monkeypatch, tmp_path, client, action="definition", symbol="foo"
        )
        assert not r.is_error
        assert "foo" in r.content

    async def test_references_empty(self, tmp_path, monkeypatch):
        client = _ToolFakeClient(references=[])
        r = await self._run(
            monkeypatch, tmp_path, client, action="references", symbol="foo"
        )
        assert not r.is_error
        assert "No references" in r.content

    async def test_references_truncated_at_50(self, tmp_path, monkeypatch):
        client = _ToolFakeClient(
            references=[_loc(f"file:///f{i}.py", i) for i in range(1, 61)]
        )
        r = await self._run(monkeypatch, tmp_path, client, action="references")
        assert not r.is_error
        assert "60 found" in r.content
        assert "and 10 more" in r.content
        assert len(r.content.splitlines()) == 1 + 50 + 1

    async def test_hover(self, tmp_path, monkeypatch):
        client = _ToolFakeClient(hover="hover text")
        r = await self._run(monkeypatch, tmp_path, client, action="hover")
        assert not r.is_error
        assert r.content == "hover text"

    async def test_runtime_error_mapped(self, tmp_path, monkeypatch):
        client = _ToolFakeClient(symbols=RuntimeError("server boom"))
        r = await self._run(monkeypatch, tmp_path, client)
        assert r.is_error
        assert "LSP error" in r.content

    async def test_timeout_mapped(self, tmp_path, monkeypatch):
        client = _ToolFakeClient(symbols=TimeoutError())
        r = await self._run(monkeypatch, tmp_path, client)
        assert r.is_error
        assert "timed out" in r.content

    async def test_generic_exception_mapped(self, tmp_path, monkeypatch):
        client = _ToolFakeClient(symbols=ValueError("bad"))
        r = await self._run(monkeypatch, tmp_path, client)
        assert r.is_error
        assert "LSP failed" in r.content

    async def test_server_start_failure_mapped(self, tmp_path, monkeypatch):
        async def _boom(fp):
            raise OSError("no server")

        f = tmp_path / "x.py"
        f.write_text("x = 1\n")
        monkeypatch.setattr(lsp_mod, "_get_client", _boom)
        r = await lsp_mod.lsp.fn(action="symbols", filepath=str(f))
        assert r.is_error
        assert "failed to start" in r.content

    async def test_server_missing_install_hint(self, tmp_path, monkeypatch):
        async def _none(fp):
            return None

        f = tmp_path / "x.py"
        f.write_text("x = 1\n")
        monkeypatch.setattr(lsp_mod, "_get_client", _none)
        monkeypatch.setattr(lsp_mod, "_find_lsp_command", lambda lang: None)
        r = await lsp_mod.lsp.fn(action="symbols", filepath=str(f))
        assert r.is_error
        assert "not found" in r.content


class _FakeGetClient:
    def __init__(self, client):
        self.client = client

    async def __call__(self, filepath):
        return self.client

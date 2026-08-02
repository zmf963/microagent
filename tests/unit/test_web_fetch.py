"""Tests for web_fetch builtin tool — full fetch flow, SSRF, error paths.

The `_is_blocked_ip` / `_resolve_and_check` functions are tested in
test_web_fetch_ssrf.py. This file covers the tool entry point and the
HTTP fetching paths (using httpx mocking to avoid network).
"""

import asyncio
import pytest

from microagent.tools.builtins.web_fetch import (
    _BLOCKED_HOSTNAMES,
    _resolve_and_check,
    web_fetch,
)


# --- URL validation paths (no network) ---

@pytest.mark.asyncio
async def test_unsupported_scheme():
    r = await web_fetch.fn(url="ftp://example.com/file")
    assert r.is_error
    assert "unsupported URL scheme" in r.content


@pytest.mark.asyncio
async def test_no_hostname():
    r = await web_fetch.fn(url="http://")
    assert r.is_error
    assert "no hostname" in r.content


@pytest.mark.asyncio
async def test_blank_url():
    r = await web_fetch.fn(url="   ")
    # urlparse of blank → scheme "" → unsupported
    assert r.is_error


# --- Blocked hostname / IP paths (no network) ---

@pytest.mark.asyncio
async def test_blocked_localhost_hostname(monkeypatch):
    # localhost is blocked before any DNS
    r = await web_fetch.fn(url="http://localhost:8080/path")
    assert r.is_error
    assert "blocked" in r.content.lower()


@pytest.mark.asyncio
async def test_blocked_internal_ip(monkeypatch):
    r = await web_fetch.fn(url="http://127.0.0.1:9000/")
    assert r.is_error
    assert "blocked" in r.content.lower()


@pytest.mark.asyncio
async def test_blocked_resolved_ip(monkeypatch):
    """A public hostname that resolves to an internal IP is blocked."""
    # Simulate getaddrinfo returning an internal IP
    import socket

    def fake_getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    r = await web_fetch.fn(url="http://example.com:80/")
    assert r.is_error
    assert "blocked" in r.content.lower()


@pytest.mark.asyncio
async def test_unresolvable_host(monkeypatch):
    import socket

    def fake_getaddrinfo(host, port, *a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    r = await web_fetch.fn(url="http://nonexistent.invalid/")
    assert r.is_error
    assert "cannot resolve" in r.content.lower()


# --- Successful fetch path (mock httpx) ---

class _FakeResponse:
    def __init__(self, status_code=200, reason_phrase="OK", chunks=b"<html>hello world</html>"):
        self.status_code = status_code
        self.reason_phrase = reason_phrase
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            from httpx import HTTPStatusError, Response
            resp = Response(self.status_code)
            raise HTTPStatusError("error", request=None, response=resp)

    async def aiter_bytes(self):
        yield self._chunks


class _FakeClientStream:
    def __init__(self, response):
        self._resp = response

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, response):
        self._resp = response
        self.kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kwargs):
        self.kwargs = (method, url, kwargs)
        return _FakeClientStream(self._resp)


@pytest.mark.asyncio
async def test_successful_fetch(monkeypatch):
    import httpx
    fake_client = _FakeClient(_FakeResponse(chunks=b"hello world content"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake_client)
    r = await web_fetch.fn(url="http://8.8.8.8/page")
    assert not r.is_error
    assert "hello world content" in r.content


@pytest.mark.asyncio
async def test_truncated_large_fetch(monkeypatch):
    import httpx
    big = b"x" * 50_000  # > max_chars (10_000)
    fake_client = _FakeClient(_FakeResponse(chunks=big))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake_client)
    r = await web_fetch.fn(url="http://8.8.8.8/big")
    assert not r.is_error
    assert "truncated at 10000 chars" in r.content
    assert len(r.content) < 11_000


@pytest.mark.asyncio
async def test_http_error_status(monkeypatch):
    import httpx
    fake_client = _FakeClient(_FakeResponse(status_code=404, reason_phrase="Not Found"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake_client)
    r = await web_fetch.fn(url="http://8.8.8.8/missing")
    assert r.is_error
    assert "404" in r.content


@pytest.mark.asyncio
async def test_httpx_not_installed(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "httpx":
            raise ImportError("no httpx")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = await web_fetch.fn(url="http://8.8.8.8/")
    assert r.is_error
    assert "httpx not installed" in r.content


@pytest.mark.asyncio
async def test_generic_fetch_error(monkeypatch):
    import httpx
    fake_client = _FakeClient(_FakeResponse())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: (_ for _ in ()).throw(ConnectionError("refused")))
    r = await web_fetch.fn(url="http://8.8.8.8/")
    assert r.is_error
    assert "fetch failed" in r.content

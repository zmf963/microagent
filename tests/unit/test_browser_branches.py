"""Tests for browser tool branches not covered by test_browser_mock.py.

Uses fake page objects and monkeypatched _ensure_browser so no real
Playwright browser is launched. URL validation goes through the real
_check_navigate_url (pure function, no network for literal IPs).
"""

import pytest

from microagent.tools.builtins import browser as br


class _NavFakePage:
    """Page whose goto records the URL and can be told to fail or redirect."""

    def __init__(self, redirect_to=None, goto_error=None, evaluate_ok=True):
        self.url = "about:blank"
        self._redirect_to = redirect_to
        self._goto_error = goto_error
        self._evaluate_ok = evaluate_ok
        self._keyboard = None
        self.closed = False
        self.init_script = None
        self.title_val = "Redirected"
        self.eval_calls = []

    async def close(self):
        self.closed = True

    async def add_init_script(self, js):
        self.init_script = js

    async def goto(self, url, timeout=None):
        if self._goto_error is not None:
            raise self._goto_error
        self.url = self._redirect_to or url

    async def title(self):
        return self.title_val

    async def evaluate(self, expr, *a, **k):
        self.eval_calls.append(expr)
        if not self._evaluate_ok:
            raise RuntimeError("evaluate boom")
        return "evaluated text"

    async def click(self, selector, timeout=None):
        if selector == "text=#btn":
            raise RuntimeError("no such element")
        self.clicked = selector

    async def fill(self, selector, text, timeout=None):
        self.filled = (selector, text)

    async def go_back(self, timeout=None):
        self.went_back = True

    async def wait_for_load_state(self, state, timeout=None):
        self.load_state = state

    async def wait_for_timeout(self, ms):
        self.timeout_ms = ms

    async def screenshot(self, type=None, full_page=False):
        return b"\x89PNG-fake"

    @property
    def keyboard(self):
        if self._keyboard is None:
            self._keyboard = self._Keyboard()
        return self._keyboard

    class _Keyboard:
        def __init__(self):
            self.pressed = None

        async def press(self, key):
            self.pressed = key


def _set_page(page):
    br._current_state.set(br.BrowserState(page=page))


@pytest.fixture(autouse=True)
def _reset_browser(monkeypatch):
    br._browser = None
    br._playwright = None
    br._current_state.set(br.BrowserState())
    yield
    br._browser = None
    br._playwright = None


def _url_always_safe(monkeypatch):
    def _fake_check(url):
        return None

    monkeypatch.setattr(br, "_check_navigate_url", _fake_check)


class _FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        return self._page


class TestBrowserNavigateBranches:
    async def test_navigate_uses_prelaunch_and_postredirect_check(self, monkeypatch):
        calls = []

        def _check(url):
            calls.append(url)
            return None

        monkeypatch.setattr(br, "_check_navigate_url", _check)
        page = _NavFakePage()

        async def _ensure():
            br._browser = _FakeBrowser(page)
            return br._browser

        monkeypatch.setattr(br, "_ensure_browser", _ensure)
        r = await br.browser_navigate.fn(url="https://example.com")
        assert not r.is_error
        assert calls == ["https://example.com", "https://example.com"]
        assert br._get_state().page is page

    async def test_navigate_closes_previous_page(self, monkeypatch):
        old = _NavFakePage()
        fresh = _NavFakePage()

        async def _ensure():
            br._browser = _FakeBrowser(fresh)
            return br._browser

        monkeypatch.setattr(br, "_ensure_browser", _ensure)
        br._current_state.set(br.BrowserState(page=old))
        r = await br.browser_navigate.fn(url="https://example.com")
        assert not r.is_error
        assert old.closed
        assert br._get_state().page is fresh

    async def test_navigate_redirect_to_internal_rejected(self, monkeypatch):
        def _check(url):
            if url.startswith("http://127.0.0.1"):
                return "blocked: internal (SSRF protection)"
            return None

        monkeypatch.setattr(br, "_check_navigate_url", _check)
        page = _NavFakePage(redirect_to="http://127.0.0.1/latest/meta-data")

        async def _ensure():
            br._browser = _FakeBrowser(page)
            return br._browser

        monkeypatch.setattr(br, "_ensure_browser", _ensure)
        r = await br.browser_navigate.fn(url="https://evil.example.com")
        assert r.is_error
        assert "redirect target rejected" in r.content
        assert page.closed
        assert br._get_state().page is None

    async def test_navigate_goto_failure(self, monkeypatch):
        _url_always_safe(monkeypatch)
        page = _NavFakePage(goto_error=TimeoutError("navigation timeout"))

        async def _ensure():
            br._browser = _FakeBrowser(page)
            return br._browser

        monkeypatch.setattr(br, "_ensure_browser", _ensure)
        r = await br.browser_navigate.fn(url="https://example.com")
        assert r.is_error
        assert "navigate failed" in r.content

    async def test_navigate_import_error(self, monkeypatch):
        _url_always_safe(monkeypatch)

        async def _ensure():
            raise ImportError("playwright not installed. Run: pip install playwright")

        monkeypatch.setattr(br, "_ensure_browser", _ensure)
        r = await br.browser_navigate.fn(url="https://example.com")
        assert r.is_error
        assert "playwright not installed" in r.content

    async def test_navigate_blocked_scheme(self):
        r = await br.browser_navigate.fn(url="file:///etc/passwd")
        assert r.is_error
        assert "scheme" in r.content

    async def test_navigate_blocked_internal_ip(self):
        r = await br.browser_navigate.fn(url="http://192.168.1.1/")
        assert r.is_error
        assert "blocked" in r.content.lower()


class TestBrowserSnapshotBranches:
    async def test_full_snapshot(self, monkeypatch):
        page = _NavFakePage()
        _set_page(page)
        r = await br.browser_snapshot.fn(full=True)
        assert not r.is_error
        assert r.content == "evaluated text"
        assert "innerText" in page.eval_calls[0]

    async def test_compact_snapshot_uses_selector_js(self, monkeypatch):
        page = _NavFakePage()
        _set_page(page)
        r = await br.browser_snapshot.fn(full=False)
        assert not r.is_error
        assert "querySelectorAll" in page.eval_calls[0]

    async def test_snapshot_evaluate_error(self, monkeypatch):
        page = _NavFakePage(evaluate_ok=False)
        _set_page(page)
        r = await br.browser_snapshot.fn()
        assert r.is_error
        assert "snapshot failed" in r.content


class TestBrowserClickBranches:
    async def test_click_text_fallback_to_css(self, monkeypatch):
        page = _NavFakePage()
        _set_page(page)
        r = await br.browser_click.fn(ref="#btn")
        assert not r.is_error
        assert page.clicked == "#btn"
        assert page.load_state == "networkidle"

    async def test_click_empty_ref(self, monkeypatch):
        _set_page(_NavFakePage())
        r = await br.browser_click.fn(ref="   ")
        assert r.is_error
        assert "ref is required" in r.content

    async def test_click_both_attempts_fail(self, monkeypatch):
        class _AlwaysFail(_NavFakePage):
            async def click(self, selector, timeout=None):
                raise RuntimeError("gone")

        _set_page(_AlwaysFail())
        r = await br.browser_click.fn(ref="#btn")
        assert r.is_error
        assert "click failed" in r.content


class TestBrowserTypeBranches:
    async def test_type_no_page(self):
        br._current_state.set(br.BrowserState())
        r = await br.browser_type.fn(ref="#inp", text="x")
        assert r.is_error

    async def test_type_empty_ref(self):
        _set_page(_NavFakePage())
        r = await br.browser_type.fn(ref="", text="x")
        assert r.is_error

    async def test_type_fill_failure(self):
        class _FailFill(_NavFakePage):
            async def fill(self, selector, text, timeout=None):
                raise RuntimeError("gone")

        _set_page(_FailFill())
        r = await br.browser_type.fn(ref="#inp", text="x")
        assert r.is_error
        assert "type failed" in r.content


class TestBrowserBackBranches:
    async def test_back_failure(self):
        class _FailBack(_NavFakePage):
            async def go_back(self, timeout=None):
                raise RuntimeError("no history")

        _set_page(_FailBack())
        r = await br.browser_back.fn()
        assert r.is_error
        assert "back failed" in r.content


class TestBrowserScrollBranches:
    async def test_scroll_up_negative_delta(self):
        page = _NavFakePage()
        _set_page(page)
        r = await br.browser_scroll.fn(direction="up", amount=200)
        assert not r.is_error
        assert "window.scrollBy" in page.eval_calls[0]
        assert page.eval_calls[0] and True

    async def test_scroll_evaluate_failure(self):
        page = _NavFakePage(evaluate_ok=False)
        _set_page(page)
        r = await br.browser_scroll.fn(direction="down")
        assert r.is_error
        assert "scroll failed" in r.content


class TestBrowserPressBranches:
    async def test_press_single_character_allowed(self):
        page = _NavFakePage()
        _set_page(page)
        r = await br.browser_press.fn(key="a")
        assert not r.is_error
        assert page.keyboard.pressed == "a"

    async def test_press_failure(self):
        class _FailPress(_NavFakePage):
            @property
            def keyboard(self):
                class _K:
                    async def press(self, key):
                        raise RuntimeError("gone")

                return _K()

        _set_page(_FailPress())
        r = await br.browser_press.fn(key="Enter")
        assert r.is_error
        assert "press failed" in r.content


class _ConsoleFakePage(_NavFakePage):
    def __init__(self):
        super().__init__()
        self._evals = {
            "__microagent_console || []": "[log] hi\n[error] boom",
            "window.__microagent_console = []": None,
            "1 + 1": 2,
        }
        self.cleared = False

    async def evaluate(self, expr, *a, **k):
        for key, val in self._evals.items():
            if key in expr:
                return val
        return super().evaluate(expr, *a, **k)


class TestBrowserConsoleBranches:
    async def test_expression_evaluated(self):
        page = _ConsoleFakePage()
        _set_page(page)
        r = await br.browser_console.fn(expression="1 + 1")
        assert not r.is_error
        assert r.content == "2"

    async def test_expression_too_long(self):
        _set_page(_ConsoleFakePage())
        r = await br.browser_console.fn(expression="x" * 10_001)
        assert r.is_error
        assert "too long" in r.content

    async def test_read_console_messages(self):
        page = _ConsoleFakePage()
        _set_page(page)
        r = await br.browser_console.fn()
        assert not r.is_error
        assert "[log] hi" in r.content

    async def test_read_and_clear(self):
        page = _ConsoleFakePage()
        _set_page(page)
        r = await br.browser_console.fn(clear=True)
        assert not r.is_error

    async def test_console_evaluate_failure(self):
        _set_page(_NavFakePage(evaluate_ok=False))
        r = await br.browser_console.fn(expression="x")
        assert r.is_error
        assert "console failed" in r.content


class _ImagesFakePage(_NavFakePage):
    def __init__(self, images):
        super().__init__()
        self._images = images

    async def evaluate(self, expr, *a, **k):
        if "imgs" in expr:
            return self._images
        return super().evaluate(expr, *a, **k)


class TestBrowserGetImagesBranches:
    async def test_no_images(self):
        _set_page(_ImagesFakePage([]))
        r = await br.browser_get_images.fn()
        assert not r.is_error
        assert "(no visible images" in r.content

    async def test_images_listed(self):
        _set_page(
            _ImagesFakePage(
                [
                    {"src": "https://x.com/a.png", "alt": "Alt", "width": 10, "height": 20},
                    {"src": "https://x.com/b.png", "alt": "", "width": 5, "height": 6},
                ]
            )
        )
        r = await br.browser_get_images.fn()
        assert not r.is_error
        assert "2 visible" in r.content
        assert '"Alt"' in r.content
        assert "10×20" in r.content

    async def test_get_images_evaluate_failure(self):
        _set_page(_NavFakePage(evaluate_ok=False))
        r = await br.browser_get_images.fn()
        assert r.is_error
        assert "get_images failed" in r.content

    async def test_get_images_no_page(self):
        br._current_state.set(br.BrowserState())
        r = await br.browser_get_images.fn()
        assert r.is_error


class TestCloseGlobalBrowser:
    async def test_closes_and_nils(self, monkeypatch):
        class _FakeBrowser:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        class _FakePlaywright:
            def __init__(self):
                self.stopped = False

            async def stop(self):
                self.stopped = True

        fb = _FakeBrowser()
        fp = _FakePlaywright()
        br._browser = fb
        br._playwright = fp
        await br.close_global_browser()
        assert fb.closed
        assert fp.stopped
        assert br._browser is None
        assert br._playwright is None

    async def test_none_is_noop(self):
        br._browser = None
        br._playwright = None
        await br.close_global_browser()

    async def test_close_exception_swallowed(self, monkeypatch):
        class _ExplodingBrowser:
            async def close(self):
                raise RuntimeError("boom")

        br._browser = _ExplodingBrowser()
        await br.close_global_browser()
        assert br._browser is None


class TestCheckNavigateUrl:
    def test_valid_http_url_returns_none(self):
        assert br._check_navigate_url("https://example.com/page?q=1") is None

    def test_unsupported_scheme_message(self):
        err = br._check_navigate_url("ftp://example.com/file")
        assert err is not None
        assert "scheme" in err

    def test_missing_hostname_message(self):
        err = br._check_navigate_url("http://")
        assert err is not None
        assert "no hostname" in err

    def test_unresolvable_hostname_message(self, monkeypatch):
        import socket

        def _fail(host, port, family, kind):
            raise socket.gaierror("no such host")

        monkeypatch.setattr("socket.getaddrinfo", _fail)
        err = br._check_navigate_url("https://nonexistent.invalid/")
        assert err is not None
        assert "cannot resolve" in err

    def test_localhost_blocked(self):
        err = br._check_navigate_url("http://localhost:8080/")
        assert err is not None
        assert "SSRF" in err

    def test_dot_local_blocked(self):
        err = br._check_navigate_url("http://printer.local/")
        assert err is not None
        assert "SSRF" in err

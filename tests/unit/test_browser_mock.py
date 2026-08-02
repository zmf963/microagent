"""Tests for browser tools with a mocked Playwright API.

Playwright isn't installed, so we inject a fake module into sys.modules
and fake Browser/Page objects to exercise the tool logic.
"""

import base64
import sys
import types

import pytest

from microagent.tools.builtins import browser as br


class _FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.title_val = "Test Page"
        self.closed = False
        self.snapshot_html = "<html><body><button>Click me</button><a href='/x'>Link</a></body></html>"
        self.console_msgs = []
        self.screenshot_data = b"\x89PNG-fake"

    async def close(self):
        self.closed = True

    async def add_init_script(self, js):
        self.init_script = js

    async def goto(self, url, timeout=None):
        self.url = url

    async def title(self):
        return self.title_val

    async def evaluate(self, expr, *a, **k):
        # get_images: return fake image list
        if "imgs" in expr:
            return [{"src": "https://example.com/a.png", "alt": "alt text",
                     "width": 10, "height": 20}]
        # snapshot full mode
        if "innerText" in expr:
            return "<body>test text</body>"
        return "ok"

    async def click(self, selector, timeout=None):
        self.clicked = selector

    async def fill(self, selector, text, timeout=None):
        self.filled = (selector, text)

    async def go_back(self, timeout=None):
        self.url = "about:blank"

    @property
    def mouse(self):
        class _M:
            async def wheel(self, dx, dy):
                pass
        return _M()

    @property
    def keyboard(self):
        class _K:
            async def press(self, key):
                self.pressed = key
        return _K()

    async def wait_for_load_state(self, state, timeout=None):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def screenshot(self, type=None, full_page=False):
        return self.screenshot_data

    async def query_selector_all(self, sel):
        return [type("El", (), {
            "get_attribute": lambda self, a: "/img.png" if a == "src" else "alt text",
            "bounding_box": lambda self: {"x": 0, "y": 0, "width": 10, "height": 10},
            "evaluate": lambda self, *a: 10,
        })()]

    async def evaluate_handle(self, expr):
        return type("H", (), {"json_value": lambda self: []})


class _FakeBrowser:
    async def new_page(self):
        return _FakePage()


class _FakePlaywrightInstance:
    class chromium:
        @staticmethod
        async def launch(headless=True):
            return _FakeBrowser()


class _FakePlaywright:
    async def start(self):
        return _FakePlaywrightInstance()


def _install_fake_playwright(monkeypatch):
    """Inject a fake playwright module."""
    async_api = types.ModuleType("playwright")
    async_api_mod = types.ModuleType("playwright.async_api")
    async_api_mod.async_playwright = lambda: _FakePlaywright()
    monkeypatch.setitem(sys.modules, "playwright", async_api)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_mod)


@pytest.fixture
def fake_playwright(monkeypatch):
    _install_fake_playwright(monkeypatch)
    br._browser = None
    br._playwright = None
    yield
    br._browser = None
    br._playwright = None


@pytest.mark.usefixtures("fake_playwright")
class TestBrowserTools:
    @pytest.mark.asyncio
    async def test_navigate(self):
        state = br.BrowserState()
        br._current_state.set(state)
        r = await br.browser_navigate.fn(url="https://example.com")
        assert not r.is_error
        assert "Test Page" in r.content
        assert state.page is not None
        assert state.page.url == "https://example.com"

    @pytest.mark.asyncio
    async def test_navigate_empty_url(self):
        r = await br.browser_navigate.fn(url="   ")
        assert r.is_error
        assert "url is required" in r.content

    @pytest.mark.asyncio
    async def test_snapshot_requires_page(self):
        br._current_state.set(br.BrowserState())
        r = await br.browser_snapshot.fn()
        assert r.is_error
        assert "no page" in r.content

    @pytest.mark.asyncio
    async def test_click_requires_page(self):
        br._current_state.set(br.BrowserState())
        r = await br.browser_click.fn(ref="#btn")
        assert r.is_error

    @pytest.mark.asyncio
    async def test_type_requires_page(self):
        br._current_state.set(br.BrowserState())
        r = await br.browser_type.fn(ref="#inp", text="hello")
        assert r.is_error

    @pytest.mark.asyncio
    async def test_type_click_on_page(self):
        state = br.BrowserState()
        state.page = _FakePage()
        br._current_state.set(state)
        r = await br.browser_type.fn(ref="#inp", text="hi")
        assert not r.is_error
        assert state.page.filled == ("#inp", "hi")
        r = await br.browser_click.fn(ref="#btn")
        assert not r.is_error
        assert state.page.clicked is not None

    @pytest.mark.asyncio
    async def test_click_requires_ref(self):
        br._current_state.set(br.BrowserState(page=_FakePage()))
        r = await br.browser_click.fn(ref="")
        assert r.is_error
        assert "ref is required" in r.content

    @pytest.mark.asyncio
    async def test_back(self):
        state = br.BrowserState()
        state.page = _FakePage()
        br._current_state.set(state)
        r = await br.browser_back.fn()
        assert not r.is_error

    @pytest.mark.asyncio
    async def test_back_no_page(self):
        br._current_state.set(br.BrowserState())
        r = await br.browser_back.fn()
        assert r.is_error

    @pytest.mark.asyncio
    async def test_scroll(self):
        state = br.BrowserState()
        state.page = _FakePage()
        br._current_state.set(state)
        r = await br.browser_scroll.fn(direction="down", amount=100)
        assert not r.is_error

    @pytest.mark.asyncio
    async def test_scroll_invalid_direction(self):
        state = br.BrowserState()
        state.page = _FakePage()
        br._current_state.set(state)
        r = await br.browser_scroll.fn(direction="sideways", amount=100)
        assert r.is_error

    @pytest.mark.asyncio
    async def test_press(self):
        state = br.BrowserState()
        state.page = _FakePage()
        br._current_state.set(state)
        r = await br.browser_press.fn(key="Enter")
        assert not r.is_error

    @pytest.mark.asyncio
    async def test_press_invalid_key(self):
        state = br.BrowserState()
        state.page = _FakePage()
        br._current_state.set(state)
        r = await br.browser_press.fn(key="NOT-A-REAL-KEY!")
        assert r.is_error

    @pytest.mark.asyncio
    async def test_vision(self):
        state = br.BrowserState()
        state.page = _FakePage()
        br._current_state.set(state)
        r = await br.browser_vision.fn(question="What is here?")
        assert not r.is_error
        assert "data:image/png;base64," in r.content

    @pytest.mark.asyncio
    async def test_get_images(self):
        state = br.BrowserState()
        state.page = _FakePage()
        br._current_state.set(state)
        r = await br.browser_get_images.fn()
        assert not r.is_error

    def test_interceptor_js_present(self):
        assert "window.__microagent_console" in br._CONSOLE_INTERCEPTOR_JS

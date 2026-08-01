"""Regression tests for the browser console-capture feature.

The headline bug: the console interceptor was injected via
page.evaluate() on the about:blank context, then page.goto() replaced
the document — destroying window.__microagent_console. browser_console
always returned "(no console output)". The fix uses page.add_init_script
which Playwright re-runs before every navigation's scripts execute.

Playwright is not installed in the unit-test environment, so these are
structural/source-level assertions on the navigate tool and the
interceptor JS. An end-to-end capture test lives under integration/.
"""

import inspect
import pytest

from microagent.tools.builtins import browser
from microagent.tools.builtins.browser import (
    _CONSOLE_INTERCEPTOR_JS,
    browser_navigate,
)


def _navigate_body() -> str:
    """Return the source of browser_navigate's underlying async function."""
    return inspect.getsource(browser_navigate.fn)


class TestInterceptorInjectionOrder:
    def test_add_init_script_called_before_goto(self):
        body = _navigate_body()
        # Match the actual indented call sites, not the explanatory comment
        # (the comment mentions "page.goto()" to explain the old bug).
        init_call = "await state.page.add_init_script("
        goto_call = "await state.page.goto("
        assert init_call in body, "navigate must install init script"
        assert goto_call in body, "navigate must call goto"
        assert body.index(init_call) < body.index(goto_call), (
            "add_init_script must run before goto so the interceptor survives "
            "the navigation document replacement"
        )

    def test_no_evaluate_based_console_injection_remains(self):
        """The old evaluate() injection on about:blank must be gone.

        The explanatory comment still mentions 'page.evaluate()' to document
        the old bug — that's fine. What matters is no actual call.
        """
        body = _navigate_body()
        # Match an actual call site (indented await), not the comment.
        assert "await state.page.evaluate(" not in body, (
            "navigate must not call page.evaluate() to install the interceptor "
            "(destroyed by goto). Use add_init_script."
        )


class TestInterceptorRobustness:
    def test_interceptor_js_initializes_buffer(self):
        assert "window.__microagent_console = []" in _CONSOLE_INTERCEPTOR_JS

    def test_interceptor_wraps_all_five_levels(self):
        for level in ("log", "warn", "error", "info", "debug"):
            assert level in _CONSOLE_INTERCEPTOR_JS, f"missing console.{level}"

    def test_interceptor_guards_circular_json(self):
        """JSON.stringify of a circular structure (DOM element, window) throws
        'Converting circular structure to JSON'. The interceptor must catch
        that and fall back to String(a), or logging a DOM node throws back
        into page code."""
        assert "safeStringify" in _CONSOLE_INTERCEPTOR_JS
        assert "catch" in _CONSOLE_INTERCEPTOR_JS
        # And the catch must fall back to String(a), not swallow silently
        assert "return String(a)" in _CONSOLE_INTERCEPTOR_JS


@pytest.mark.asyncio
async def test_navigate_empty_url_returns_error():
    """Guard the cheap path so a future refactor doesn't drop the guard."""
    result = await browser_navigate.fn("   ")
    assert result.is_error
    assert "url is required" in result.content

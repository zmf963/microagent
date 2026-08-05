"""browser tools — Playwright-based web browsing.

Provides 10 tools: navigate, snapshot, click, type, back, scroll,
press, console, images, and vision (screenshot).

Uses a per-session page state via ContextVar to maintain browser state
across tool calls within a session, providing isolation between
concurrent Agent sessions.

The Playwright/browser instances are module-level (expensive to create,
shared across sessions). Each session gets its own page via ContextVar.

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

import anyio
from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from .._session_state import session_state

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Playwright

# Module-level browser instances (expensive to create, shared across sessions)
_browser: Browser | None = None
_playwright: Playwright | None = None
_lock = anyio.Lock()


async def close_global_browser() -> None:
    """Shut down the shared Chromium instance and Playwright runtime.

    Called from Agent.close() so short-lived embeddings (create/destroy
    per task) don't leak headless Chromium processes.
    """
    global _browser, _playwright
    async with _lock:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception:
                pass
            _playwright = None


def _check_navigate_url(url: str) -> str | None:
    """Pre-launch URL validation. Returns an error string or None.

    - scheme whitelist: http/https only. file:// leaks arbitrary local
      files through browser_snapshot (bypassing read_file's permission
      layer); javascript:/data: are script-injection vectors.
    - SSRF: same blocklist as web_fetch (loopback, RFC 1918, CGNAT,
      link-local, .local hostnames) — the browser must not become a
      backdoor around web_fetch's protections.
    """
    from urllib.parse import urlparse

    from .web_fetch import _resolve_and_check

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported URL scheme: {parsed.scheme!r}. Only http/https allowed."
    host = parsed.hostname
    if not host:
        return f"invalid URL: no hostname found in {url!r}"
    return _resolve_and_check(host)


# ---------------------------------------------------------------------------
# Per-session browser state (ContextVar)
# ---------------------------------------------------------------------------


@dataclass
class BrowserState:
    """Per-session browser page state."""

    page: Page | None = None


_current_state, _get_state = session_state(
    "browser_current_state", BrowserState,
)


async def _ensure_browser():
    """Lazily initialize Playwright and Chromium (shared across sessions)."""
    global _playwright, _browser
    if _browser is not None:
        return
    async with _lock:
        if _browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError(
                "playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
    # After the lock, _browser is guaranteed non-None
    assert _browser is not None
    return _browser

def _require_page() -> Page:
    state = _get_state()
    if state.page is None:
        raise RuntimeError("no page open — call browser_navigate first")
    return state.page


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


# Console interceptor JS, installed via page.add_init_script so it runs
# BEFORE any page document loads and survives client-side navigations / SPA
# route changes. Wraps console.log/warn/error/info/debug into a captured
# buffer that browser_console can read. Guards JSON.stringify against
# circular structures (DOM elements, event objects) so logging them does
# not throw back into page code.
_CONSOLE_INTERCEPTOR_JS = r"""
window.__microagent_console = [];
(function () {
    var orig = {
        log: console.log, warn: console.warn, error: console.error,
        info: console.info, debug: console.debug,
    };
    function safeStringify(a) {
        if (typeof a === 'object' && a !== null) {
            try { return JSON.stringify(a); }
            catch (e) { return String(a); }
        }
        return String(a);
    }
    for (var level in orig) {
        (function (level, fn) {
            console[level] = function () {
                var args = Array.prototype.slice.call(arguments);
                window.__microagent_console.push({
                    level: level,
                    text: args.map(safeStringify).join(' '),
                    ts: Date.now()
                });
                fn.apply(console, args);
            };
        })(level, orig[level]);
    }
})();
"""


@tool("browser_navigate", description="Open a URL in the browser. Must be called first.")
async def browser_navigate(
    url: Annotated[str, Field(description="URL to navigate to")],
) -> ToolResult:
    if not url.strip():
        return ToolResult.error("url is required")

    url_error = await asyncio.to_thread(_check_navigate_url, url)
    if url_error is not None:
        return ToolResult.error(url_error)

    try:
        await _ensure_browser()
        browser = _browser
        assert browser is not None
        state = _get_state()
        if state.page is not None:
            await state.page.close()
        state.page = await browser.new_page()

        # Install the console interceptor as an init script so Playwright
        # re-runs it on every navigation BEFORE page scripts execute.
        # (Previously this was injected via page.evaluate() on about:blank,
        #  then page.goto() replaced the document and silently destroyed the
        #  interceptor — browser_console never captured anything.)
        await state.page.add_init_script(_CONSOLE_INTERCEPTOR_JS)

        await state.page.goto(url, timeout=30000)
        title = await state.page.title()
        return ToolResult.ok(f"Opened: {title}\n{state.page.url}")
    except ImportError as e:
        return ToolResult.error(str(e))
    except Exception as e:
        return ToolResult.error(f"navigate failed: {e!r}")


@tool("browser_snapshot", description="Get a text-based snapshot of the current page showing interactive elements.")
async def browser_snapshot(
    full: Annotated[
        bool, Field(description="If true, return complete page text. Default: compact (interactive elements only).")
    ] = False,
) -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))

    try:
        if full:
            text = await page.evaluate(
                "() => (document.body?.innerText || '(empty page)').substring(0, 10000)"
            )
        else:
            text = await page.evaluate("""() => {
                const body = document.body;
                if (!body) return '(empty page)';
                const interactive = ['a','button','input','select','textarea',
                    '[role=button]','[role=link]','[role=textbox]','[role=combobox]'];
                const els = body.querySelectorAll(interactive.join(','));
                if (!els.length) return body.innerText.substring(0, 5000);
                const seen = new Set();
                const parts = [];
                for (let i = 0; i < els.length; i++) {
                    const el = els[i];
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const text = (el.textContent || '').trim().substring(0, 200);
                    if (!text) continue;
                    // Dedupe by position, not text — two buttons with same
                    // label are different elements.
                    const posKey = Math.round(rect.x) + ',' + Math.round(rect.y);
                    if (seen.has(posKey)) continue;
                    seen.add(posKey);
                    const tag = el.tagName.toLowerCase();
                    const id = el.id ? '#' + el.id : '';
                    const cls = el.className && typeof el.className === 'string'
                        ? '.' + el.className.split(' ').slice(0,2).join('.') : '';
                    const href = tag === 'a' && el.href ? ' → ' + el.href.substring(0, 60) : '';
                    parts.push('[' + tag + id + cls + '] ' + text + href);
                }
                return parts.join('\\n').substring(0, 5000) || '(no interactive elements)';
            }""")
        return ToolResult.ok(text)
    except Exception as e:
        return ToolResult.error(f"snapshot failed: {e!r}")


@tool("browser_click", description="Click an element by CSS selector, ref ID from snapshot, or visible text.")
async def browser_click(
    ref: Annotated[
        str, Field(description="CSS selector (e.g. '#id', '.class', 'button'), ref ID (@e5), or link text")
    ],
) -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))
    if not ref.strip():
        return ToolResult.error("ref is required")

    try:
        # Try text= first, then CSS selector
        try:
            await page.click(f"text={ref}", timeout=5000)
        except Exception:
            await page.click(ref, timeout=5000)
        await page.wait_for_load_state("networkidle", timeout=10000)
        title = await page.title()
        return ToolResult.ok(f"Clicked '{ref}'. Current page: {title}")
    except Exception as e:
        return ToolResult.error(f"click failed: {e!r}")


@tool("browser_type", description="Type text into an input field identified by CSS selector.")
async def browser_type(
    ref: Annotated[str, Field(description="CSS selector for the input field")],
    text: Annotated[str, Field(description="Text to type")],
) -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))
    if not ref.strip():
        return ToolResult.error("ref is required")

    try:
        await page.fill(ref, text, timeout=5000)
        return ToolResult.ok(f"Typed '{text}' into {ref}")
    except Exception as e:
        return ToolResult.error(f"type failed: {e!r}")


@tool("browser_back", description="Navigate back to the previous page.")
async def browser_back() -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))

    try:
        await page.go_back(timeout=15000)
        title = await page.title()
        return ToolResult.ok(f"Back to: {title}\n{page.url}")
    except Exception as e:
        return ToolResult.error(f"back failed: {e!r}")


@tool("browser_scroll", description="Scroll the page up or down.")
async def browser_scroll(
    direction: Annotated[str, Field(description="'up' or 'down'")],
    amount: Annotated[int, Field(description="Pixels to scroll", ge=1, le=10000)] = 500,
) -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))

    if direction not in ("up", "down"):
        return ToolResult.error("direction must be 'up' or 'down'")

    delta = amount if direction == "down" else -amount
    try:
        await page.evaluate("([delta]) => window.scrollBy(0, delta)", [delta])
        return ToolResult.ok(f"Scrolled {direction} {amount}px")
    except Exception as e:
        return ToolResult.error(f"scroll failed: {e!r}")


_VALID_KEYS = frozenset({
    "Enter", "Tab", "Escape", "Backspace", "Delete",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "PageUp", "PageDown", "Home", "End",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
    "Control", "Alt", "Shift", "Meta",
    "Space", "Insert",
})


@tool("browser_press", description="Press a keyboard key (Enter, Tab, Escape, ArrowDown, etc.).")
async def browser_press(
    key: Annotated[str, Field(description="Key to press: Enter, Tab, Escape, ArrowDown, ArrowUp, Backspace, etc.")],
) -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))

    if key not in _VALID_KEYS and len(key) > 1:
        return ToolResult.error(
            f"invalid key: {key}. Use a named key (Enter, Tab, Escape, ArrowDown, etc.) "
            f"or a single character."
        )

    try:
        await page.keyboard.press(key)
        return ToolResult.ok(f"Pressed: {key}")
    except Exception as e:
        return ToolResult.error(f"press failed: {e!r}")


@tool("browser_console", description="Get browser console output or evaluate JavaScript on the page.")
async def browser_console(
    expression: Annotated[
        str, Field(description="JavaScript expression to evaluate. Omit to read console messages.")
    ] = "",
    clear: Annotated[bool, Field(description="If true, clear console message buffer after reading")] = False,
) -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))

    try:
        if expression:
            # Limit expression size to prevent browser OOM
            if len(expression) > 10_000:
                return ToolResult.error(
                    f"expression too long: {len(expression)} chars exceeds 10000 limit"
                )
            result = await page.evaluate(expression)
            import json as _json
            return ToolResult.ok(_json.dumps(result, default=str, ensure_ascii=False))
        else:
            # Collect recent console messages via a listener pattern
            text = await page.evaluate("""() => {
                const msgs = window.__microagent_console || [];
                return msgs.slice(-50).map(m => '[' + m.level + '] ' + m.text).join('\\n') || '(no console output)';
            }""")
            if clear:
                await page.evaluate("() => { window.__microagent_console = []; }")
            return ToolResult.ok(text)
    except Exception as e:
        return ToolResult.error(f"console failed: {e!r}")


@tool("browser_get_images", description="Get a list of images on the current page with URLs and alt text.")
async def browser_get_images(
    max_results: Annotated[int, Field(description="Maximum images to return", ge=1, le=50)] = 20,
) -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))

    try:
        images = await page.evaluate("""([maxResults]) => {
            const imgs = document.querySelectorAll('img[src]');
            const results = [];
            for (const img of imgs) {
                if (results.length >= maxResults) break;
                const rect = img.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                results.push({{
                    src: img.src.substring(0, 200),
                    alt: (img.alt || '').substring(0, 100),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                }});
            }}
            return results;
        }""", [max_results])
        if not images:
            return ToolResult.ok("(no visible images on page)")
        lines = [f"Images on page ({len(images)} visible):"]
        for i, img in enumerate(images):
            alt = f' "{img["alt"]}"' if img["alt"] else ""
            lines.append(
                f"  {i+1}. {img['width']}×{img['height']}{alt}\n"
                f"     {img['src']}"
            )
        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.error(f"get_images failed: {e!r}")


@tool("browser_vision", description="Take a screenshot of the current page for visual inspection.")
async def browser_vision(
    question: Annotated[str, Field(description="What to look for in the screenshot")] = "Describe this page.",
    annotate: Annotated[
        bool, Field(description="If true, overlay numbered labels on interactive elements")
    ] = False,
) -> ToolResult:
    try:
        page = _require_page()
    except RuntimeError as e:
        return ToolResult.error(str(e))

    try:
        if annotate:
            # Inject numbered labels on interactive elements for spatial reasoning
            await page.evaluate("""() => {
                document.querySelectorAll('.microagent-label').forEach(el => el.remove());
                const els = document.querySelectorAll('a,button,input,select,textarea,[role=button]');
                els.forEach((el, i) => {
                    if (i > 99) return;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return;
                    const label = document.createElement('div');
                    label.className = 'microagent-label';
                    label.textContent = '' + (i + 1);
                    Object.assign(label.style, {
                        position: 'fixed', left: rect.left + 'px', top: rect.top + 'px',
                        background: 'red', color: 'white', padding: '1px 3px',
                        fontSize: '10px', fontWeight: 'bold', zIndex: '99999',
                        pointerEvents: 'none', borderRadius: '2px',
                    });
                    document.body.appendChild(label);
                });
            }""")
            await page.wait_for_timeout(200)

        try:
            screenshot = await page.screenshot(type="png", full_page=False)
            b64 = base64.b64encode(screenshot).decode()
        finally:
            # Clean up labels even if screenshot throws (page closed,
            # navigation interrupted, OOM rendering huge page). Without
            # finally, the numbered red overlays persist on the page and
            # pollute every subsequent snapshot/vision call.
            if annotate:
                try:
                    await page.evaluate(
                        "() => document.querySelectorAll('.microagent-label').forEach(el => el.remove())"
                    )
                except Exception:
                    pass  # page may already be gone

        # Return as data URL so vision-capable models can see it
        return ToolResult.ok(
            f"[Screenshot captured: {len(screenshot)} bytes]\n"
            f"Question: {question}\n\n"
            f"data:image/png;base64,{b64}"
        )
    except Exception as e:
        return ToolResult.error(f"vision failed: {e!r}")

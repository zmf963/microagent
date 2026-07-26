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

import base64
import contextvars
from dataclasses import dataclass
from typing import Annotated

import anyio
from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# Module-level browser instances (expensive to create, shared across sessions)
_browser: object = None
_playwright: object = None
_lock = anyio.Lock()


# ---------------------------------------------------------------------------
# Per-session browser state (ContextVar)
# ---------------------------------------------------------------------------


@dataclass
class BrowserState:
    """Per-session browser page state."""

    page: object = None  # Playwright Page | None


_current_state: contextvars.ContextVar[BrowserState | None] = contextvars.ContextVar(
    "browser_current_state", default=None
)


def _get_state() -> BrowserState:
    state = _current_state.get()
    if state is None:
        state = BrowserState()
        _current_state.set(state)
    return state


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


def _require_page() -> object:
    state = _get_state()
    if state.page is None:
        raise RuntimeError("no page open — call browser_navigate first")
    return state.page


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool("browser_navigate", description="Open a URL in the browser. Must be called first.")
async def browser_navigate(
    url: Annotated[str, Field(description="URL to navigate to")],
) -> ToolResult:
    if not url.strip():
        return ToolResult.error("url is required")

    try:
        await _ensure_browser()
        state = _get_state()
        if state.page is not None:
            await state.page.close()
        state.page = await _browser.new_page()

        # Inject console capture — intercepts console.* calls into
        # window.__microagent_console so browser_console can read them.
        await state.page.evaluate("""() => {
            window.__microagent_console = [];
            const orig = {
                log: console.log, warn: console.warn, error: console.error,
                info: console.info, debug: console.debug,
            };
            for (const [level, fn] of Object.entries(orig)) {
                console[level] = function() {
                    window.__microagent_console.push({
                        level, text: Array.from(arguments).map(a =>
                            typeof a === 'object' ? JSON.stringify(a) : String(a)
                        ).join(' '),
                        ts: Date.now()
                    });
                    fn.apply(console, arguments);
                };
            }
        }""")

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
        await page.evaluate(f"window.scrollBy(0, {delta})")
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
        images = await page.evaluate(f"""() => {{
            const imgs = document.querySelectorAll('img[src]');
            const results = [];
            for (const img of imgs) {{
                if (results.length >= {max_results}) break;
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
        }}""")
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

        screenshot = await page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(screenshot).decode()

        if annotate:
            # Clean up labels
            await page.evaluate(
                "() => document.querySelectorAll('.microagent-label').forEach(el => el.remove())"
            )

        # Return as data URL so vision-capable models can see it
        return ToolResult.ok(
            f"[Screenshot captured: {len(screenshot)} bytes]\n"
            f"Question: {question}\n\n"
            f"data:image/png;base64,{b64}"
        )
    except Exception as e:
        return ToolResult.error(f"vision failed: {e!r}")

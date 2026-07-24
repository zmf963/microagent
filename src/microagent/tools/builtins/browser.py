"""browser tools — Playwright-based web browsing.

Provides navigate, snapshot, click, and type operations.
Uses a per-session page state via ContextVar to maintain browser state
across tool calls within a session, providing isolation between
concurrent Agent sessions.

The Playwright/browser instances are module-level (expensive to create,
shared across sessions). Each session gets its own page via ContextVar.

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

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
# Per-session browser state (ContextVar — same pattern as process.py)
# ---------------------------------------------------------------------------


@dataclass
class BrowserState:
    """Per-session browser page state."""

    page: object = None  # Playwright Page | None


_current_state: contextvars.ContextVar[BrowserState | None] = contextvars.ContextVar(
    "browser_current_state", default=None
)


def _get_state() -> BrowserState:
    """Get the current session's browser state.

    When running inside a SessionRunner, the ContextVar is set to the
    runner's state. When called directly (e.g., in tests without a
    runner), a temporary state is lazily created and stored.
    """
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
        await state.page.goto(url, timeout=30000)
        title = await state.page.title()
        return ToolResult.ok(f"Opened: {title}\n{state.page.url}")
    except ImportError as e:
        return ToolResult.error(str(e))
    except Exception as e:
        return ToolResult.error(f"navigate failed: {e!r}")


@tool("browser_snapshot", description="Get a text snapshot of the current page.")
async def browser_snapshot() -> ToolResult:
    state = _get_state()
    if state.page is None:
        return ToolResult.error("no page open — call browser_navigate first")

    try:
        # Extract text content and interactive elements
        text = await state.page.evaluate("""() => {
            const body = document.body;
            if (!body) return '(empty page)';
            // Get all visible text
            const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
            const parts = [];
            let node;
            while (node = walker.nextNode()) {
                const text = node.textContent.trim();
                if (text && node.parentElement) {
                    const tag = node.parentElement.tagName.toLowerCase();
                    const rect = node.parentElement.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        if (['a','button','input','select','textarea'].includes(tag)) {
                            parts.push('[' + tag + '] ' + text);
                        } else {
                            parts.push(text);
                        }
                    }
                }
            }
            return parts.join('\\n').substring(0, 5000) || '(no visible text)';
        }""")
        return ToolResult.ok(text)
    except Exception as e:
        return ToolResult.error(f"snapshot failed: {e!r}")


@tool("browser_click", description="Click an element by its CSS selector or text content.")
async def browser_click(
    ref: Annotated[
        str, Field(description="CSS selector (e.g. '#id', '.class', 'button') or link text")
    ],
) -> ToolResult:
    state = _get_state()
    if state.page is None:
        return ToolResult.error("no page open — call browser_navigate first")
    if not ref.strip():
        return ToolResult.error("ref is required")

    try:
        # Try CSS selector first, then text match
        try:
            await state.page.click(ref, timeout=5000)
        except Exception:
            await state.page.click(f"text={ref}", timeout=5000)
        await state.page.wait_for_load_state("networkidle", timeout=10000)
        title = await state.page.title()
        return ToolResult.ok(f"Clicked '{ref}'. Current page: {title}")
    except Exception as e:
        return ToolResult.error(f"click failed: {e!r}")


@tool("browser_type", description="Type text into an input field identified by CSS selector.")
async def browser_type(
    ref: Annotated[str, Field(description="CSS selector for the input field")],
    text: Annotated[str, Field(description="Text to type")],
) -> ToolResult:
    state = _get_state()
    if state.page is None:
        return ToolResult.error("no page open — call browser_navigate first")
    if not ref.strip():
        return ToolResult.error("ref is required")

    try:
        await state.page.fill(ref, text, timeout=5000)
        return ToolResult.ok(f"Typed '{text}' into {ref}")
    except Exception as e:
        return ToolResult.error(f"type failed: {e!r}")

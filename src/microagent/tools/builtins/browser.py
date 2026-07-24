"""browser tools — Playwright-based web browsing.

Provides navigate, snapshot, click, and type operations.
Uses a module-level Playwright session to maintain browser state
across tool calls within a session.

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import threading
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# Module-level browser state — guarded by _lock for concurrent safety
_page: object = None
_browser: object = None
_playwright: object = None
_lock = threading.Lock()


async def _get_page() -> object | None:
    """Get or raise if no page is open."""
    return _page


async def _ensure_browser():
    """Lazily initialize Playwright and Chromium."""
    global _playwright, _browser
    if _browser is not None:
        return
    with _lock:
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
    global _page
    if not url.strip():
        return ToolResult.error("url is required")

    try:
        await _ensure_browser()
        if _page is not None:
            await _page.close()
        _page = await _browser.new_page()
        await _page.goto(url, timeout=30000)
        title = await _page.title()
        return ToolResult.ok(f"Opened: {title}\n{_page.url}")
    except ImportError as e:
        return ToolResult.error(str(e))
    except Exception as e:
        return ToolResult.error(f"navigate failed: {e!r}")


@tool("browser_snapshot", description="Get a text snapshot of the current page.")
async def browser_snapshot() -> ToolResult:
    if _page is None:
        return ToolResult.error("no page open — call browser_navigate first")

    try:
        # Extract text content and interactive elements
        text = await _page.evaluate("""() => {
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
    if _page is None:
        return ToolResult.error("no page open — call browser_navigate first")
    if not ref.strip():
        return ToolResult.error("ref is required")

    try:
        # Try CSS selector first, then text match
        try:
            await _page.click(ref, timeout=5000)
        except Exception:
            await _page.click(f"text={ref}", timeout=5000)
        await _page.wait_for_load_state("networkidle", timeout=10000)
        title = await _page.title()
        return ToolResult.ok(f"Clicked '{ref}'. Current page: {title}")
    except Exception as e:
        return ToolResult.error(f"click failed: {e!r}")


@tool("browser_type", description="Type text into an input field identified by CSS selector.")
async def browser_type(
    ref: Annotated[str, Field(description="CSS selector for the input field")],
    text: Annotated[str, Field(description="Text to type")],
) -> ToolResult:
    if _page is None:
        return ToolResult.error("no page open — call browser_navigate first")
    if not ref.strip():
        return ToolResult.error("ref is required")

    try:
        await _page.fill(ref, text, timeout=5000)
        return ToolResult.ok(f"Typed '{text}' into {ref}")
    except Exception as e:
        return ToolResult.error(f"type failed: {e!r}")

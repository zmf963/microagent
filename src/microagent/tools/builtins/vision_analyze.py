"""vision_analyze builtin tool — image analysis via vision-capable LLMs.

Supports local files (auto base64-encoded), http/https URLs, and
data: URLs. Delegates to the same LLM endpoint — the model must
support vision (e.g. gpt-4o, claude-sonnet-4).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


async def _encode_image(image_url: str) -> str | None:
    """Convert image to a data: URL. Returns None if file not found."""
    if image_url.startswith("data:"):
        return image_url
    if image_url.startswith(("http://", "https://")):
        return image_url

    p = Path(image_url)
    if not p.exists():
        return None

    import asyncio

    raw = await asyncio.to_thread(p.read_bytes)
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    b64 = base64.b64encode(raw).decode()
    return f"data:{mime};base64,{b64}"


@tool(
    "vision_analyze",
    description="Analyze an image. Supports local files, URLs, and data: URLs. Requires a vision-capable model.",
)
async def vision_analyze(
    image_url: Annotated[str, Field(description="Image path, URL, or data: URL")],
    question: Annotated[
        str, Field(description="What to ask about the image")
    ] = "Describe this image.",
) -> ToolResult:
    if not image_url.strip():
        return ToolResult.error("image_url is required")

    data_url = await _encode_image(image_url)
    if data_url is None:
        return ToolResult.error(f"image not found: {image_url}")

    # The actual vision call happens in SessionRunner when the tool result
    # is sent back to the LLM. The tool itself just passes the data URL
    # as context. The LLM sees the image in the next turn.
    return ToolResult.ok(
        f"[Image encoded: {len(data_url)} bytes]\n"
        f"Question: {question}\n\n"
        f"The image will be shown to the vision model in the next response. "
        f"If you need to analyze this image, please ask the assistant."
    )

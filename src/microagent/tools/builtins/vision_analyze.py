"""vision_analyze builtin tool — image analysis via vision-capable LLMs.

Returns the image as a base64 data URL embedded in the tool result,
so the LLM can see it on the next turn if it supports vision (e.g.
gpt-4o, claude-sonnet-4).  Non-vision models will receive the data
URL as text and may not process it correctly.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult


_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB raw; base64 inflates ~4/3


async def _encode_image(image_url: str) -> str | None:
    """Convert image to a data: URL. Returns None if file not found.

    Raises ValueError for directories and oversized files — callers must
    convert these into ToolResult.error (read_bytes on a directory escapes
    IsADirectoryError, and an uncapped read lets a 100MB file become a
    ~139MB data URL inside the ToolResult content).
    """
    if image_url.startswith("data:"):
        return image_url
    if image_url.startswith(("http://", "https://")):
        return image_url

    p = Path(image_url)
    if not p.exists():
        return None
    if not p.is_file():
        raise ValueError(f"not a file: {image_url}")
    size = p.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"image too large: {size:,} bytes exceeds {_MAX_IMAGE_BYTES:,} byte limit"
        )

    import asyncio

    raw = await asyncio.to_thread(p.read_bytes)
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    b64 = base64.b64encode(raw).decode()
    return f"data:{mime};base64,{b64}"


@tool(
    "vision_analyze",
    description="Load an image for analysis. Returns a data URL the vision model can see.",
)
async def vision_analyze(
    image_url: Annotated[str, Field(description="Image path, URL, or data: URL")],
    question: Annotated[
        str, Field(description="What to ask about the image")
    ] = "Describe this image.",
) -> ToolResult:
    if not image_url.strip():
        return ToolResult.error("image_url is required")

    try:
        data_url = await _encode_image(image_url)
    except ValueError as e:
        return ToolResult.error(str(e))
    if data_url is None:
        return ToolResult.error(f"image not found: {image_url}")

    # Return the image data URL as the tool result content so the
    # LLM can see it on the next turn.  This works with vision-
    # capable models (gpt-4o, claude-sonnet-4, etc.) that understand
    # inline data: URLs in conversation context.
    return ToolResult.ok(
        f"[vision_analyze] {question}\n\n{data_url}"
    )

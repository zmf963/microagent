"""Tests for vision_analyze builtin tool."""

import base64
import pytest
from pathlib import Path
from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


class TestVisionAnalyze:
    async def test_registered_as_builtin(self):
        registry = ToolRegistry(_default_builtins())
        assert "vision_analyze" in registry.names

    async def test_empty_url_returns_error(self):
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(id="c1", name="vision_analyze", arguments={
            "image_url": "",
            "question": "what is this?",
        })
        result = await registry.execute(call)
        assert result.is_error

    async def test_local_image_base64_encode(self, tmp_path):
        """Local image file is base64-encoded before sending."""
        # Create a tiny valid PNG
        img_path = tmp_path / "test.png"
        # 1x1 red pixel PNG
        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
        )
        img_path.write_bytes(png_data)

        from microagent.tools.builtins.vision_analyze import _encode_image
        result = await _encode_image(str(img_path))
        assert result is not None
        assert result.startswith("data:image/png;base64,")

    async def test_data_url_passthrough(self):
        """data: URLs are passed through unchanged."""
        from microagent.tools.builtins.vision_analyze import _encode_image
        data_url = "data:image/png;base64,iVBORw0KGgo="
        result = await _encode_image(data_url)
        assert result == data_url

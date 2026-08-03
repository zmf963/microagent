"""Tests for vision_analyze builtin tool."""

import base64

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


class TestVisionAnalyze:
    async def test_registered_as_builtin(self):
        registry = ToolRegistry(_default_builtins())
        assert "vision_analyze" in registry.names

    async def test_empty_url_returns_error(self):
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(
            id="c1",
            name="vision_analyze",
            arguments={
                "image_url": "",
                "question": "what is this?",
            },
        )
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

    async def test_directory_path_returns_error(self, tmp_path):
        """Directory path must yield ToolResult.error, not escape IsADirectoryError."""
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(
            id="c2",
            name="vision_analyze",
            arguments={"image_url": str(tmp_path), "question": "q"},
        )
        result = await registry.execute(call)
        assert result.is_error
        assert "not a file" in result.content.lower() or "not an image" in result.content.lower()

    async def test_oversized_image_returns_error(self, tmp_path, monkeypatch):
        """Images above the size cap are rejected before base64 inflation
        (a 100MB file would become a ~139MB data URL in the ToolResult)."""
        import microagent.tools.builtins.vision_analyze as mod

        monkeypatch.setattr(mod, "_MAX_IMAGE_BYTES", 100)
        img = tmp_path / "big.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 200)

        registry = ToolRegistry(_default_builtins())
        call = ToolCall(
            id="c3",
            name="vision_analyze",
            arguments={"image_url": str(img), "question": "q"},
        )
        result = await registry.execute(call)
        assert result.is_error
        assert "too large" in result.content.lower()

"""Tests for context7 builtin tool."""

from microagent.core.tool import ToolRegistry, _default_builtins
from microagent.core.types import ToolCall


class TestContext7:
    async def test_registered_as_builtin(self):
        registry = ToolRegistry(_default_builtins())
        assert "context7" in registry.names

    async def test_empty_query(self):
        registry = ToolRegistry(_default_builtins())
        call = ToolCall(id="c1", name="context7", arguments={"query": ""})
        result = await registry.execute(call)
        assert result.is_error

    async def test_parse_response(self):
        """Response parser extracts title and snippet."""
        from microagent.tools.builtins.context7 import _parse_results

        data = {
            "results": [
                {
                    "title": "FastAPI Router",
                    "snippet": "Use APIRouter to organize routes.",
                    "url": "https://fastapi.tiangolo.com/tutorial/bigger-applications/",
                    "library": "fastapi",
                },
                {
                    "title": "Pydantic Models",
                    "snippet": "Define data models with BaseModel.",
                    "url": "https://docs.pydantic.dev/latest/concepts/models/",
                    "library": "pydantic",
                },
            ]
        }
        text = _parse_results(data, max_results=5)
        assert "FastAPI Router" in text
        assert "Pydantic Models" in text
        assert "fastapi.tiangolo.com" in text

    async def test_parse_empty(self):
        from microagent.tools.builtins.context7 import _parse_results

        text = _parse_results({"results": []}, max_results=5)
        assert text == "(no results)"

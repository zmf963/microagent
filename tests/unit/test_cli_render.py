"""Tests for the CLI rendering layer — Live Markdown region + thinking visibility.

These cover the surface/cli.py changes that fix:
  * content wrap/indent corruption (now rendered via a Rich ``Live`` region)
  * reasoning deltas shown twice / too noisy (now hidden by default, toggleable)
"""

import pytest

from microagent.agent import Agent
from microagent.core.tool import ToolRegistry
from microagent.core.types import Message, TextDelta, Usage
from microagent.llm.client import StreamDone
from microagent.session.runner import SessionRunner

# Rich render output pads lines to the console width with trailing spaces.
# Strip captured text before substring checks.
from microagent.surface.cli import (  # noqa: E402
    ReplState,
    _cmd_thinking,
    _resolve_show_thinking,
    _run_streaming,
    console,
)

from .fake_llm import FakeLLMClient, ScriptedResponse

CONTENT_MD = "**你好**！\n\n- 列表一 中文\n- 列表二 😊"
THINKING_TEXT = "让我想想该怎么做"


def _agent_with(events):
    llm = FakeLLMClient([ScriptedResponse(events=list(events))])
    runner = SessionRunner(llm=llm, registry=ToolRegistry())
    return Agent(runner=runner, registry=ToolRegistry())


def _thinking_then_content_response():
    return [
        TextDelta(text=THINKING_TEXT, kind="thinking"),
        TextDelta(text=CONTENT_MD, kind="content"),
        Usage(input_tokens=10, output_tokens=5),
        StreamDone(usage=Usage(input_tokens=10, output_tokens=5), stop_reason="stop"),
    ]


def _norm(text: str) -> str:
    """Normalise captured output: drop ANSI padding/trailing spaces per line."""
    return "\n".join(line.rstrip() for line in text.splitlines())


class TestContentLiveRendering:
    """Content must be rendered as Markdown (no raw markers) and not duplicated."""

    async def test_markdown_rendered_not_raw(self):
        agent = _agent_with(_thinking_then_content_response())
        with console.capture() as cap:
            await _run_streaming(agent, [Message.user("hi")], show_thinking=False)
        out = _norm(cap.get())
        # Markdown markers stripped by Rich → proves Live Markdown rendering
        assert "**" not in out
        # CJK + emoji preserved (no wrap corruption)
        assert "你好" in out
        assert "列表一" in out
        assert "😊" in out
        # List rendered as bullets (Rich Markdown), not a literal hyphen run
        assert "•" in out

    async def test_content_not_duplicated(self):
        agent = _agent_with(_thinking_then_content_response())
        with console.capture() as cap:
            await _run_streaming(agent, [Message.user("hi")], show_thinking=False)
        out = cap.get()
        # A distinctive content token must appear exactly once (no double print)
        assert out.count("列表一") == 1

    async def test_chunked_streaming_joins_correctly(self):
        """Content arriving in many small deltas must still render as one block."""
        parts = ["你好", "世界", "！\n\n- ", "A", " 😊"]
        events = [TextDelta(text=p, kind="content") for p in parts]
        events += [
            Usage(input_tokens=10, output_tokens=5),
            StreamDone(usage=Usage(input_tokens=10, output_tokens=5), stop_reason="stop"),
        ]
        agent = _agent_with(events)
        with console.capture() as cap:
            await _run_streaming(agent, [Message.user("hi")], show_thinking=False)
        out = _norm(cap.get())
        assert "你好世界！" in out
        assert "•" in out
        assert "😊" in out


class TestThinkingVisibility:
    async def test_thinking_hidden_by_default(self):
        agent = _agent_with(_thinking_then_content_response())
        with console.capture() as cap:
            await _run_streaming(agent, [Message.user("hi")], show_thinking=False)
        out = cap.get()
        assert THINKING_TEXT not in out

    async def test_thinking_shown_when_enabled(self):
        agent = _agent_with(_thinking_then_content_response())
        with console.capture() as cap:
            await _run_streaming(agent, [Message.user("hi")], show_thinking=True)
        out = cap.get()
        assert THINKING_TEXT in out
        # Content still present alongside thinking
        assert "你好" in out


class TestResolveShowThinking:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv("MICROAGENT_SHOW_THINKING", raising=False)
        monkeypatch.setattr(
            "microagent.surface.cli.Config._config_path", lambda: __import__("pathlib").Path("/nonexistent")
        )
        assert _resolve_show_thinking(None) is False

    def test_cli_flag_wins(self, monkeypatch):
        monkeypatch.setenv("MICROAGENT_SHOW_THINKING", "1")
        assert _resolve_show_thinking(False) is False
        assert _resolve_show_thinking(True) is True

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_env_truthy(self, monkeypatch, val):
        monkeypatch.setenv("MICROAGENT_SHOW_THINKING", val)
        monkeypatch.setattr(
            "microagent.surface.cli.Config._config_path", lambda: __import__("pathlib").Path("/nonexistent")
        )
        assert _resolve_show_thinking(None) is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_env_falsy(self, monkeypatch, val):
        monkeypatch.setenv("MICROAGENT_SHOW_THINKING", val)
        monkeypatch.setattr(
            "microagent.surface.cli.Config._config_path", lambda: __import__("pathlib").Path("/nonexistent")
        )
        assert _resolve_show_thinking(None) is False

    def test_config_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MICROAGENT_SHOW_THINKING", raising=False)
        cfg = tmp_path / "config.yaml"
        cfg.write_text("display:\n  show_thinking: true\n")
        monkeypatch.setattr("microagent.surface.cli.Config._config_path", lambda: cfg)
        assert _resolve_show_thinking(None) is True


class TestThinkingCommand:
    async def test_toggle(self):
        from unittest.mock import MagicMock

        state = ReplState(
            agent=MagicMock(),
            config=MagicMock(),
            store=MagicMock(),
            session_id="s",
        )
        assert state.show_thinking is False
        with console.capture() as cap:
            await _cmd_thinking(state, "")
        assert state.show_thinking is True
        assert "shown" in cap.get()

        with console.capture() as cap:
            await _cmd_thinking(state, "")
        assert state.show_thinking is False
        assert "hidden" in cap.get()

    async def test_explicit_on_off(self):
        from unittest.mock import MagicMock

        state = ReplState(
            agent=MagicMock(),
            config=MagicMock(),
            store=MagicMock(),
            session_id="s",
        )
        await _cmd_thinking(state, "on")
        assert state.show_thinking is True
        await _cmd_thinking(state, "off")
        assert state.show_thinking is False

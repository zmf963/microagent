"""Tests for task and question builtin tools."""

import pytest

from microagent.tools.builtins.task import _current_runner, task
from microagent.tools.builtins.question import question


class TestTask:
    @pytest.mark.asyncio
    async def test_no_runner(self):
        _current_runner.set(None)
        r = await task.fn(goal="do something")
        assert r.is_error
        assert "runner not available" in r.content

    @pytest.mark.asyncio
    async def test_unknown_subagent_type(self, monkeypatch):
        class _Runner:  # just needs to be non-None
            pass
        _current_runner.set(_Runner())
        # Force the manager.spawn to raise KeyError for unknown type
        from microagent.tools.builtins import task as task_mod
        async def _spawn(subagent_type, prompt, runner):
            raise KeyError(subagent_type)
        monkeypatch.setattr(task_mod._manager, "spawn", _spawn)
        r = await task.fn(goal="x", subagent_type="nonexistent")
        assert r.is_error
        assert "unknown subagent type" in r.content

    @pytest.mark.asyncio
    async def test_spawn_error(self, monkeypatch):
        class _Runner:
            pass
        _current_runner.set(_Runner())
        from microagent.tools.builtins import task as task_mod
        async def _spawn(subagent_type, prompt, runner):
            raise RuntimeError("spawn failed")
        monkeypatch.setattr(task_mod._manager, "spawn", _spawn)
        r = await task.fn(goal="x")
        assert r.is_error
        assert "subagent failed" in r.content

    @pytest.mark.asyncio
    async def test_goal_with_context_builds_prompt(self, monkeypatch):
        """Verify context is prefixed into the prompt passed to spawn."""
        class _Runner:
            pass
        _current_runner.set(_Runner())
        from microagent.tools.builtins import task as task_mod
        captured = {}

        async def _spawn(subagent_type, prompt, runner):
            captured["prompt"] = prompt
            captured["type"] = subagent_type
            return "subagent result"

        monkeypatch.setattr(task_mod._manager, "spawn", _spawn)
        r = await task.fn(goal="summarize", subagent_type="explore", context="here is background")
        assert not r.is_error
        assert captured["prompt"].startswith("Context:\nhere is background")
        assert "summarize" in captured["prompt"]
        assert captured["type"] == "explore"


class TestQuestion:
    @pytest.mark.asyncio
    async def test_non_interactive_returns_error(self, monkeypatch):
        """In non-TTY (programmatic) mode, question returns an error."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        r = await question.fn(text="What do you want?")
        assert r.is_error
        assert "not running in interactive mode" in r.content
        assert "What do you want?" in r.content

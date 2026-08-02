"""Tests for todo / task_plan / exit builtin tools (in-memory state)."""

import pytest

from microagent.tools.builtins.todo_plan_exit import (
    SessionState,
    _current_state,
    exit,
    plan,
    todo,
)


@pytest.fixture
def fresh_state():
    """Reset the per-session ContextVar so each test starts clean."""
    state = SessionState()
    _current_state.set(state)
    return state


class TestTodo:
    @pytest.mark.asyncio
    async def test_add_and_list(self, fresh_state):
        r = await todo.fn(action="add", content="write tests")
        assert not r.is_error
        assert "#0" in r.content
        r = await todo.fn(action="list")
        assert not r.is_error
        assert "write tests" in r.content
        assert "pending" in r.content

    @pytest.mark.asyncio
    async def test_add_requires_content(self, fresh_state):
        r = await todo.fn(action="add", content="")
        assert r.is_error
        assert "content is required" in r.content

    @pytest.mark.asyncio
    async def test_empty_list(self, fresh_state):
        r = await todo.fn(action="list")
        assert not r.is_error
        assert "(no todos)" in r.content

    @pytest.mark.asyncio
    async def test_update(self, fresh_state):
        await todo.fn(action="add", content="task one")
        r = await todo.fn(action="update", item_id=0, content="task one updated", status="completed")
        assert not r.is_error
        listed = await todo.fn(action="list")
        assert "task one updated" in listed.content
        assert "completed" in listed.content

    @pytest.mark.asyncio
    async def test_update_not_found(self, fresh_state):
        r = await todo.fn(action="update", item_id=99, content="x")
        assert r.is_error
        assert "not found" in r.content

    @pytest.mark.asyncio
    async def test_remove(self, fresh_state):
        await todo.fn(action="add", content="doomed")
        r = await todo.fn(action="remove", item_id=0)
        assert not r.is_error
        assert "doomed" in r.content
        listed = await todo.fn(action="list")
        assert "(no todos)" in listed.content

    @pytest.mark.asyncio
    async def test_remove_not_found(self, fresh_state):
        r = await todo.fn(action="remove", item_id=5)
        assert r.is_error

    @pytest.mark.asyncio
    async def test_unknown_action(self, fresh_state):
        r = await todo.fn(action="bogus")
        assert r.is_error
        assert "unknown action" in r.content


class TestPlan:
    @pytest.mark.asyncio
    async def test_set_and_show(self, fresh_state):
        r = await plan.fn(action="set", steps="step1\nstep2\nstep3")
        assert not r.is_error
        assert "3 steps" in r.content
        r = await plan.fn(action="show")
        assert not r.is_error
        assert "step1" in r.content and "step3" in r.content

    @pytest.mark.asyncio
    async def test_show_empty(self, fresh_state):
        r = await plan.fn(action="show")
        assert "(no plan set)" in r.content

    @pytest.mark.asyncio
    async def test_set_requires_steps(self, fresh_state):
        r = await plan.fn(action="set", steps="   ")
        assert r.is_error
        assert "steps is required" in r.content

    @pytest.mark.asyncio
    async def test_clear(self, fresh_state):
        await plan.fn(action="set", steps="a\nb")
        r = await plan.fn(action="clear")
        assert "cleared" in r.content
        shown = await plan.fn(action="show")
        assert "(no plan set)" in shown.content

    @pytest.mark.asyncio
    async def test_unknown_action(self, fresh_state):
        r = await plan.fn(action="bogus")
        assert r.is_error


class TestExit:
    @pytest.mark.asyncio
    async def test_exit_returns_sentinel(self, fresh_state):
        r = await exit.fn()
        assert not r.is_error
        assert "[SESSION_EXIT]" in r.content

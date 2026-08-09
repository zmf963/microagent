"""Tests for Budget — tree-shaped resource tracking."""

import pytest

from microagent.session.budget import Budget, BudgetExceeded


class TestBudgetLimits:
    def test_not_exhausted_initially(self):
        b = Budget(max_iterations=10, max_tokens=1000, max_cost_usd=1.0)
        assert not b.exhausted

    async def test_exhausted_after_consume(self):
        b = Budget(max_iterations=1)
        with pytest.raises(BudgetExceeded):
            await b.consume(iterations=1)

    async def test_exhausted_tokens(self):
        b = Budget(max_tokens=100)
        with pytest.raises(BudgetExceeded):
            await b.consume(tokens=100)

    async def test_exhausted_cost(self):
        b = Budget(max_cost_usd=0.5)
        with pytest.raises(BudgetExceeded):
            await b.consume(cost_usd=0.5)

    async def test_partial_consume(self):
        b = Budget(max_iterations=10)
        await b.consume(iterations=3)
        assert b._used_iter == 3
        assert not b.exhausted

    async def test_remaining(self):
        b = Budget(max_iterations=10)
        await b.consume(iterations=4)
        assert b.remaining == 6

    async def test_summary(self):
        b = Budget(max_iterations=25, max_tokens=1000, max_cost_usd=2.0)
        await b.consume(iterations=5, tokens=200, cost_usd=0.5)
        s = b.summary()
        assert "iterations=5/25" in s
        assert "$0.5000/$2" in s

    async def test_reset(self):
        b = Budget(max_iterations=10)
        await b.consume(iterations=8)
        assert b.remaining == 2
        b.reset()
        assert b.remaining == 10
        assert b._used_iter == 0


class TestBudgetTree:
    async def test_spawn_child(self):
        root = Budget.root(max_iterations=30, max_tokens=3000, max_cost_usd=3.0)
        child = root.spawn()
        assert child.max_iterations <= root.max_iterations
        assert child._parent is root
        assert child._cancel_event is root._cancel_event

    async def test_child_consumption_reports_to_parent(self):
        root = Budget.root(max_cost_usd=10.0)
        child = root.spawn(max_cost_usd=2.0)
        await child.consume(cost_usd=1.0)
        assert root._descendants_cost == 1.0

    async def test_child_exhaustion_does_not_set_root_cancel(self):
        """A child exhausting its OWN sub-limit must raise locally without
        killing the whole tree — the shared cancel_event is for ROOT
        exhaustion only (file-header contract). Previously a subagent
        hitting its 10-iteration cap cancelled the parent's next consume()
        with 'budget cancelled by root'."""
        root = Budget.root(max_cost_usd=10.0)
        child = root.spawn(max_cost_usd=0.5)
        with pytest.raises(BudgetExceeded):
            await child.consume(cost_usd=0.5)
        assert not root._cancel_event.is_set()
        # Parent and siblings remain usable
        await root.consume(cost_usd=1.0)
        sibling = root.spawn(max_cost_usd=0.5)
        await sibling.consume(cost_usd=0.1)

    async def test_tree_exhausted_triggers_cancel(self):
        root = Budget.root(max_cost_usd=5.0)
        c1 = root.spawn(max_cost_usd=10.0)
        c2 = root.spawn(max_cost_usd=10.0)
        await c1.consume(cost_usd=3.0)
        with pytest.raises(BudgetExceeded):
            await c2.consume(cost_usd=3.0)
        assert root._cancel_event.is_set()

    async def test_remaining_accounts_descendants(self):
        root = Budget.root(max_iterations=30)
        child = root.spawn(max_iterations=10)
        await child.consume(iterations=5)
        # root.remaining_iterations = 30 - 0(self) - 5(descendants) = 25
        assert root.remaining_iterations == 25


class TestBudgetReset:
    async def test_reset_clears_cancel_event(self):
        """reset() must clear a set cancel_event — otherwise the 'reset'
        budget keeps raising 'budget cancelled by root' forever."""
        root = Budget.root(max_cost_usd=0.5)
        with pytest.raises(BudgetExceeded):
            await root.consume(cost_usd=1.0)
        assert root._cancel_event.is_set()
        root.reset()
        assert not root._cancel_event.is_set()
        await root.consume(cost_usd=0.1)  # usable again


class TestBudgetErrorMessage:
    async def test_message_reports_all_metrics(self):
        """BudgetExceeded message must report iterations/tokens/cost so the
        triggering metric is identifiable. Previously it only reported cost,
        which was misleading when iterations exhausted the budget."""
        from microagent.session.budget import Budget, BudgetExceeded
        b = Budget.root(max_iterations=2, max_cost_usd=1000.0)
        try:
            for _ in range(3):
                await b.consume(iterations=1)
            raise AssertionError("should have raised")
        except BudgetExceeded as e:
            msg = str(e)
            assert "iterations=2/2" in msg
            assert "cost" in msg
            assert "tokens" in msg

"""Tests for Budget — tree-shaped resource tracking."""

import pytest
from microagent.session.budget import Budget, BudgetExceeded


class TestBudgetLimits:
    def test_not_exhausted_initially(self):
        b = Budget(max_iterations=10, max_tokens=1000, max_cost_usd=1.0)
        assert not b.exhausted

    def test_exhausted_after_consume(self):
        b = Budget(max_iterations=1)
        with pytest.raises(BudgetExceeded):
            b.consume(iterations=1)

    def test_exhausted_tokens(self):
        b = Budget(max_tokens=100)
        with pytest.raises(BudgetExceeded):
            b.consume(tokens=100)

    def test_exhausted_cost(self):
        b = Budget(max_cost_usd=0.5)
        with pytest.raises(BudgetExceeded):
            b.consume(cost_usd=0.5)

    def test_partial_consume(self):
        b = Budget(max_iterations=10)
        b.consume(iterations=3)
        assert b._used_iter == 3
        assert not b.exhausted

    def test_remaining(self):
        b = Budget(max_iterations=10)
        b.consume(iterations=4)
        assert b.remaining == 6

    def test_summary(self):
        b = Budget(max_iterations=25, max_tokens=1000, max_cost_usd=2.0)
        b.consume(iterations=5, tokens=200, cost_usd=0.5)
        s = b.summary()
        assert "iterations=5/25" in s
        assert "$0.5000/$2" in s

    def test_reset(self):
        b = Budget(max_iterations=10)
        b.consume(iterations=8)
        assert b.remaining == 2
        b.reset()
        assert b.remaining == 10
        assert b._used_iter == 0


class TestBudgetTree:
    def test_spawn_child(self):
        root = Budget.root(max_iterations=30, max_tokens=3000, max_cost_usd=3.0)
        child = root.spawn()
        assert child.max_iterations <= root.max_iterations
        assert child._parent is root
        assert child._cancel_event is root._cancel_event

    def test_child_consumption_reports_to_parent(self):
        root = Budget.root(max_cost_usd=10.0)
        child = root.spawn(max_cost_usd=2.0)
        child.consume(cost_usd=1.0)
        assert root._descendants_cost == 1.0

    def test_child_exhaustion_sets_root_cancel(self):
        root = Budget.root(max_cost_usd=10.0)
        child = root.spawn(max_cost_usd=0.5)
        with pytest.raises(BudgetExceeded):
            child.consume(cost_usd=0.5)
        assert root._cancel_event.is_set()

    def test_tree_exhausted_triggers_cancel(self):
        root = Budget.root(max_cost_usd=5.0)
        c1 = root.spawn(max_cost_usd=10.0)
        c2 = root.spawn(max_cost_usd=10.0)
        c1.consume(cost_usd=3.0)
        with pytest.raises(BudgetExceeded):
            c2.consume(cost_usd=3.0)
        assert root._cancel_event.is_set()

    def test_remaining_accounts_descendants(self):
        root = Budget.root(max_iterations=30)
        child = root.spawn(max_iterations=10)
        child.consume(iterations=5)
        # root.remaining_iterations = 30 - 0(self) - 5(descendants) = 25
        assert root.remaining_iterations == 25

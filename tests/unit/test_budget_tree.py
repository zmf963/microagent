"""Tests for tree-shaped Budget (spawn, cancel_event, descendants tracking)."""

import pytest

from microagent.session.budget import Budget, BudgetExceeded


class TestBudgetTree:
    def test_spawn_inherits_parent_limits(self):
        root = Budget.root(max_iterations=30, max_tokens=300_000, max_cost_usd=10.0)
        child = root.spawn(max_iterations=5, max_cost_usd=1.0)
        assert child.max_iterations == 5
        assert child.max_cost_usd == 1.0
        # tokens default: parent remaining / 3
        assert child.max_tokens > 0

    def test_spawn_default_one_third(self):
        root = Budget.root(max_iterations=30, max_tokens=300_000, max_cost_usd=10.0)
        child = root.spawn()  # all defaults → 1/3 of parent remaining
        assert child.max_iterations == 10  # 30 // 3
        assert child.max_cost_usd == 10.0 / 3

    def test_child_consume_reports_to_parent(self):
        root = Budget.root(max_iterations=30, max_cost_usd=10.0)
        child = root.spawn(max_iterations=5, max_cost_usd=2.0)
        child.consume(iterations=2, cost_usd=0.5)
        # Parent's remaining should reflect child's consumption
        assert root.remaining_cost == 10.0 - 0.5

    def test_root_exhaustion_cancels_children(self):
        root = Budget.root(max_iterations=10, max_cost_usd=5.0, max_tokens=999_999)
        child = root.spawn(max_iterations=20, max_cost_usd=10.0)  # more generous
        # Exhaust root
        with pytest.raises(BudgetExceeded):
            root.consume(iterations=10, cost_usd=5.0)
        # Child should now throw because root cancel_event is set
        with pytest.raises(BudgetExceeded, match="cancelled"):
            child.consume(iterations=1)

    def test_child_self_exhaustion(self):
        child = Budget(max_iterations=3, max_cost_usd=1.0, max_tokens=999_999)
        child.consume(iterations=2)
        assert not child.exhausted
        with pytest.raises(BudgetExceeded):
            child.consume(iterations=1)
        assert child.exhausted

    def test_spawn_from_non_root(self):
        """spawn() from a non-root budget works but uses its own limits."""
        b = Budget(max_iterations=20, max_tokens=200_000, max_cost_usd=5.0)
        child = b.spawn(max_iterations=3)
        assert child.max_iterations == 3
        # Non-root: no cancel_event, no parent chain
        child.consume(iterations=1)
        assert child.remaining == 2

    def test_root_method(self):
        root = Budget.root(max_iterations=10, max_tokens=100_000, max_cost_usd=3.0)
        assert root.max_iterations == 10
        assert root.max_cost_usd == 3.0
        # root has a cancel_event
        assert root._cancel_event is not None

"""Budget — tree-shaped resource tracking with shared cancel_event.

M3b version: adds spawn() for subagent budgets, parent-child
descendants tracking, and a shared cancel_event for root exhaustion.
"""

from __future__ import annotations

from dataclasses import dataclass

import anyio


@dataclass
class Budget:
    """Tree-shaped budget: own limits + descendants accumulator + shared cancel."""

    max_iterations: int = 25
    max_tokens: int = 200_000
    max_cost_usd: float = 5.0

    _used_iter: int = 0
    _used_tokens: int = 0
    _used_cost: float = 0.0

    # Parent chain for ancestor reporting
    _parent: Budget | None = None

    # Shared cancel signal: all descendants of the same root share one event
    _cancel_event: anyio.Event | None = None

    # Descendant accumulated usage (excludes own _used_*)
    _descendants_cost: float = 0.0
    _descendants_tokens: int = 0
    _descendants_iter: int = 0

    @classmethod
    def root(cls, **limits) -> Budget:
        """Create a root budget with a shared cancel_event."""
        return cls(_cancel_event=anyio.Event(), **limits)

    def spawn(
        self,
        *,
        max_iterations: int | None = None,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> Budget:
        """Spawn a child budget. Defaults to 1/3 of parent remaining."""
        rem_cost = self.remaining_cost
        rem_tok = self.remaining_tokens
        rem_iter = self.remaining_iterations

        return Budget(
            max_iterations=min(
                max_iterations if max_iterations is not None else max(1, rem_iter // 3),
                rem_iter,
            ),
            max_tokens=min(
                max_tokens if max_tokens is not None else max(1, rem_tok // 3),
                rem_tok,
            ),
            max_cost_usd=min(
                max_cost_usd if max_cost_usd is not None else max(0.01, rem_cost / 3),
                rem_cost,
            ),
            _parent=self,
            _cancel_event=self._cancel_event,  # share root's cancel event
        )

    @property
    def exhausted(self) -> bool:
        return (
            self._used_iter >= self.max_iterations
            or self._used_tokens >= self.max_tokens
            or self._used_cost >= self.max_cost_usd
        )

    @property
    def remaining(self) -> int:
        """Remaining iterations (self only, not counting descendants)."""
        return max(0, self.max_iterations - self._used_iter)

    @property
    def remaining_iterations(self) -> int:
        """Remaining iterations accounting for descendants."""
        return max(0, self.max_iterations - self._used_iter - self._descendants_iter)

    @property
    def remaining_tokens(self) -> int:
        """Remaining tokens accounting for descendants."""
        return max(0, self.max_tokens - self._used_tokens - self._descendants_tokens)

    @property
    def remaining_cost(self) -> float:
        """Remaining cost accounting for descendants."""
        return max(0.0, self.max_cost_usd - self._used_cost - self._descendants_cost)

    def consume(
        self,
        *,
        iterations: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Consume budget. Reports to parent chain. Sets cancel_event on exhaustion."""
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise BudgetExceeded("budget cancelled by root")

        self._used_iter += iterations
        self._used_tokens += tokens
        self._used_cost += cost_usd

        # Report to ancestor chain
        node = self._parent
        while node is not None:
            node._descendants_iter += iterations
            node._descendants_tokens += tokens
            node._descendants_cost += cost_usd
            node = node._parent

        if self.exhausted or self._tree_exhausted():
            if self._cancel_event is not None:
                self._cancel_event.set()  # notify entire tree
            raise BudgetExceeded(
                "budget exhausted: "
                f"self_cost={self._used_cost:.4f}/{self.max_cost_usd}, "
                f"tree_cost={self._tree_cost_used():.4f}/{self._root_max_cost():.4f}"
            )

    def _tree_exhausted(self) -> bool:
        """Whether the root's total (self + all descendants) is over limit."""
        root = self._root()
        total_cost = root._used_cost + root._descendants_cost
        total_tokens = root._used_tokens + root._descendants_tokens
        return total_cost >= root.max_cost_usd or total_tokens >= root.max_tokens

    def _root(self) -> Budget:
        node = self
        while node._parent is not None:
            node = node._parent
        return node

    def _root_max_cost(self) -> float:
        return self._root().max_cost_usd

    def _tree_cost_used(self) -> float:
        root = self._root()
        return root._used_cost + root._descendants_cost

    def summary(self) -> str:
        return (
            f"iterations={self._used_iter}/{self.max_iterations}, "
            f"tokens={self._used_tokens}/{self.max_tokens}, "
            f"cost=${self._used_cost:.4f}/${self.max_cost_usd}"
        )

    def reset(self) -> None:
        self._used_iter = 0
        self._used_tokens = 0
        self._used_cost = 0.0
        self._descendants_iter = 0
        self._descendants_tokens = 0
        self._descendants_cost = 0.0


class BudgetExceeded(Exception):
    """Raised when budget limits are exceeded."""

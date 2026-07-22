"""Budget — tracks iteration, token, and cost usage per turn.

M0b-2 version: adds tokens + cost_usd tracking.
Full tree-shaped Budget (spawn / shared cancel_event / descendants) arrives in M3.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Budget:
    """Tracks resource consumption for a single turn."""
    max_iterations: int = 25
    max_tokens: int = 200_000
    max_cost_usd: float = 5.0

    _used_iter: int = 0
    _used_tokens: int = 0
    _used_cost: float = 0.0

    # Optional: per-iteration token estimate (for cost calculation)
    # Updated by SessionRunner after each LLM response.

    @property
    def exhausted(self) -> bool:
        return (
            self._used_iter >= self.max_iterations
            or self._used_tokens >= self.max_tokens
            or self._used_cost >= self.max_cost_usd
        )

    @property
    def remaining(self) -> int:
        """Remaining iterations."""
        return max(0, self.max_iterations - self._used_iter)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self._used_tokens)

    @property
    def remaining_cost(self) -> float:
        return max(0.0, self.max_cost_usd - self._used_cost)

    def consume(
        self,
        *,
        iterations: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Consume budget. Raises BudgetExceeded if any limit hit."""
        self._used_iter += iterations
        self._used_tokens += tokens
        self._used_cost += cost_usd

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


class BudgetExceeded(Exception):
    """Raised when budget limits are exceeded."""

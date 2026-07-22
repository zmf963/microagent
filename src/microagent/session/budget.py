"""Minimal Budget for M0a — iteration counting only.

Full tree-shaped Budget (tokens / cost / spawn / shared cancel_event)
arrives in M0b / Appendix C.6.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Budget:
    """Simple iteration limiter for M0a."""
    max_iterations: int = 25
    _used: int = 0

    @property
    def exhausted(self) -> bool:
        return self._used >= self.max_iterations

    @property
    def remaining(self) -> int:
        return self.max_iterations - self._used

    def consume(self, iterations: int = 1) -> None:
        self._used += iterations

    def reset(self) -> None:
        self._used = 0

"""RetryPolicy — per-provider retry behavior resolved at client construction.

deepseek-harness parity: the policy travels WITH the route/provider
(captured at client construction), not as a global. A global retryable-
codes frozenset cannot express "this gateway 500s when overloaded but
recovers instantly — retry aggressively" vs "this provider's 500 is a
bug — don't retry at all".

Policy vocabulary:
  normal   — retry retryable failures once, then give up (default)
  always   — retry retryable failures up to max_retries
  never    — never retry (even transient failures fail the turn)
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RETRYABLE_CODES

# ---------------------------------------------------------------------------
# Backoff configuration (shared with client.py's SDK-level backoff)
# ---------------------------------------------------------------------------

MAX_BACKOFF_RETRIES = 3
BACKOFF_BASE = 2.0  # exponential base
BACKOFF_JITTER = 0.25  # ±25% jitter


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Per-provider retry behavior.

    ``mode``: normal | always | never
    ``max_retries``: retry ceiling for mode='always' (default 3)
    """

    mode: str = "normal"
    max_retries: int = 3

    def __post_init__(self):
        if self.mode not in ("normal", "always", "never"):
            raise ValueError(
                f"invalid retry mode {self.mode!r} — use normal|always|never"
            )
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")

    @classmethod
    def from_str(cls, spec: str) -> RetryPolicy:
        """Parse 'normal', 'always:5', 'never' etc.

        Invalid specs raise ValueError — silently coercing 'alwayz' to
        'normal' changed production retry behavior with no signal. The
        constructor raises for bad modes; from_str must match.
        """
        spec = spec.strip()
        if spec.startswith("always"):
            parts = spec.split(":", 1)
            n = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 3
            if len(parts) > 1 and not parts[1].strip().isdigit():
                raise ValueError(f"invalid retry spec {spec!r}: always:N needs an integer N")
            return cls(mode="always", max_retries=n)
        if spec in ("normal", "never"):
            return cls(mode=spec)
        raise ValueError(f"invalid retry mode {spec!r} — use normal|always[:N]|never")

    def allows_retry(self, failure_code: str, attempts_so_far: int) -> bool:
        """Whether one more retry is permitted for this failure.

        ``attempts_so_far`` = retries already consumed for this logical
        call. mode='normal' allows exactly one retry; 'always' up to
        max_retries; 'never' none. Non-retryable codes always return
        False (auth/bad-request retries burn budget for nothing).
        """
        if failure_code not in RETRYABLE_CODES:
            return False
        if self.mode == "never":
            return False
        if self.mode == "always":
            return attempts_so_far < self.max_retries
        return attempts_so_far < 1  # normal: one-shot retry

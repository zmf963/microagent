"""LLM failure classification — machine-readable error taxonomy.

deepseek-harness parity: LlmFailure{code, status, retryAfterMs} normalizes
every adapter failure into a stable code so callers branch on the code,
never on free-text messages. MicroAgent maps stream-phase exceptions into
the same shape; the runner decides retry eligibility from the code.
"""

from __future__ import annotations

from dataclasses import dataclass

# Code vocabulary (subset of dsh's LlmFailureCode + one local addition).
RETRYABLE_CODES = frozenset({
    "timeout",            # idle watchdog / transport timeout — transient
    "rate_limit",         # 429 / provider Retry-After
    "overloaded",         # 529 / provider busy
    "server_error",       # 5xx
    "network_error",      # connection reset, DNS — transient
    "empty_response",     # stream ended with no content — dsh treats as retryable
})

NON_RETRYABLE_CODES = frozenset({
    "auth_error",         # 401/403 — retrying the same key is pointless
    "bad_request",        # 400 — payload is wrong, retry cannot fix it
    "context_exceeded",   # input too long — fix context, not retry
    "aborted",            # caller cancelled
    "unknown",            # unrecognized failure
})


@dataclass(frozen=True, slots=True)
class LLMFailure:
    """Normalized LLM call failure.

    ``code`` is one of the RETRYABLE/NON_RETRYABLE vocabularies;
    ``message`` is the original human-readable text; ``status`` is the
    HTTP status when available.
    """

    code: str
    message: str
    status: int | None = None

    @property
    def is_retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def classify_exception(exc: BaseException) -> LLMFailure:
    """Map a stream-phase exception to an LLMFailure.

    HTTP status is extracted from httpx/openai exception shapes when
    present; everything else falls to ``unknown``.
    """
    message = str(exc) or repr(exc)

    if isinstance(exc, (KeyboardInterrupt,)):
        return LLMFailure("aborted", message)

    status: int | None = None
    for attr in ("status_code", "status"):
        if hasattr(exc, attr):
            try:
                status = int(getattr(exc, attr))
            except (TypeError, ValueError):
                continue
            break

    if status is not None:
        if status == 401 or status == 403:
            return LLMFailure("auth_error", message, status)
        if status == 429:
            return LLMFailure("rate_limit", message, status)
        if status in (400, 404, 422):
            return LLMFailure("bad_request", message, status)
        if status == 413:
            return LLMFailure("context_exceeded", message, status)
        if status in (408, 504):
            return LLMFailure("timeout", message, status)
        if status == 529:
            return LLMFailure("overloaded", message, status)
        if 500 <= status < 600:
            return LLMFailure("server_error", message, status)

    name = type(exc).__name__
    lowered = f"{name}: {message}".lower()
    if "timeout" in lowered or "timed out" in lowered:
        return LLMFailure("timeout", message, status)
    if "rate" in lowered and "limit" in lowered:
        return LLMFailure("rate_limit", message, status)
    if "overload" in lowered:
        return LLMFailure("overloaded", message, status)
    if "connection" in lowered or "resolve" in lowered or "eof" in lowered \
            or "reset" in lowered:
        return LLMFailure("network_error", message, status)
    if "context" in lowered and ("exceed" in lowered or "too long" in lowered
                                 or "maximum" in lowered):
        return LLMFailure("context_exceeded", message, status)
    if "empty" in lowered:
        return LLMFailure("empty_response", message, status)
    if "auth" in lowered or "unauthorized" in lowered or "forbidden" in lowered \
            or "api key" in lowered:
        return LLMFailure("auth_error", message, status)
    return LLMFailure("unknown", message, status)

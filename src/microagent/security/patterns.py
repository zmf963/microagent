"""Security: injection pattern scanning for system prompt and context content.

Minimal version: tag-based blacklist + [BLOCKED] replacement + NFKC
normalization. NFKC collapses visually-similar Unicode codepoints
(e.g. full-width < → half-width <) that could otherwise bypass the
pattern matchers.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Patterns that indicate injection attempts
_INJECTION_PATTERNS = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE),
    re.compile(r"</system>", re.IGNORECASE),
    re.compile(r"<system\b[^>]*>", re.IGNORECASE),
    re.compile(r"<context>.*?</context>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<memory-context>.*?</memory-context>", re.DOTALL | re.IGNORECASE),
]

_BLOCKED_PLACEHOLDER = "[BLOCKED: injection pattern detected]"


@dataclass(frozen=True, slots=True)
class InjectionResult:
    """Result of scanning text for injection patterns."""

    blocked: bool
    sanitized: str
    reason: str = ""


def scan_for_injection(text: str) -> InjectionResult:
    """Scan text for injection patterns after NFKC normalization.

    Normalization collapses full-width / half-width variants (e.g.
    ``<ｓystem>`` → ``<system>``) so pattern matchers catch visually
    disguised injection attempts.

    Returns InjectionResult with blocked=True if any pattern matched.
    The sanitized field contains the text with injections replaced by [BLOCKED].
    """
    if not text:
        return InjectionResult(blocked=False, sanitized=text)

    # NFKC normalization — collapses full-width CJK punctuation,
    # compatibility characters, and other visually-similar glyphs.
    normalized = unicodedata.normalize("NFKC", text)

    sanitized = normalized
    matched_patterns: list[str] = []

    for pattern in _INJECTION_PATTERNS:
        matches = pattern.findall(normalized)
        if matches:
            matched_patterns.append(pattern.pattern)
            sanitized = pattern.sub(_BLOCKED_PLACEHOLDER, sanitized)

    if matched_patterns:
        return InjectionResult(
            blocked=True,
            sanitized=sanitized,
            reason=f"detected: {', '.join(matched_patterns)}",
        )

    return InjectionResult(blocked=False, sanitized=text)

"""Secret scrubbing for memory persistence.

Raw conversation excerpts that flow through MemoryExtractor into the
persistent memory store (and are re-injected into future prompts via
recall) must not carry credentials: an API key pasted in chat once
would otherwise live in a 500-row LRU memory and resurface in every
future session.

The patterns are deliberately conservative — they redact only the
VALUE of well-known key shapes and credential-looking assignments, so
ordinary prose survives untouched.
"""

from __future__ import annotations

import re

# Well-known credential token shapes (prefix + sufficient entropy).
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),  # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),  # Google
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),  # JWT
)

# KEY=VALUE / "api_key": "value" assignments — redact only the value.
# The key matcher accepts any identifier CONTAINING a credential word
# (API_TOKEN, OPENAI_API_KEY, db_password…); over-redaction is the safe
# direction for persistent memory.
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<key>[A-Za-z0-9_-]*(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|"
    r"token|secret|password|passwd|pwd|bearer)[A-Za-z0-9_-]*)\b"
    r"(?P<sep>\s*[:=]\s*)(?P<q>[\"']?)[^\s\"',;)]+(?P=q)"
)

_REDACTED = "[REDACTED]"


def scrub_secrets(text: str) -> str:
    """Return ``text`` with credential-shaped substrings redacted.

    Conservative by design: hits are replaced with ``[REDACTED]``; any
    text that matches nothing is returned unchanged.
    """
    result = text
    for pat in _TOKEN_PATTERNS:
        result = pat.sub(_REDACTED, result)
    result = _ASSIGNMENT_RE.sub(
        lambda m: (
            f"{m.group('key')}{m.group('sep')}{m.group('q')}{_REDACTED}{m.group('q')}"
        ),
        result,
    )
    return result

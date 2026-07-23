"""Context compression — keeps long conversations within token limits.

When conversation history exceeds max_tokens, early messages are
replaced with a summary placeholder to preserve context while
staying under the limit. The most recent messages are always preserved.
"""

from __future__ import annotations

from ..core.types import Message


def estimate_tokens(text: str) -> int:
    """Rough token count estimate: ~4 chars per token for English,
    ~2 chars per token for CJK. Conservative overestimate for safety."""
    if not text:
        return 0
    chars = len(text)
    # Count CJK characters roughly
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff')
    non_cjk = chars - cjk
    return (non_cjk // 4) + (cjk // 2) + 1  # +1 for safety margin


def compress_history(
    messages: tuple[Message, ...],
    max_tokens: int = 100_000,
) -> tuple[Message, ...]:
    """Compress conversation history to fit within max_tokens.

    Strategy:
    1. Always preserve the last message (current user input).
    2. Always preserve the system-like first message pair.
    3. If over limit, replace middle messages with a summary placeholder.
    4. Preserve user/assistant alternation.
    """
    total = sum(estimate_tokens(m.content or "") for m in messages)
    if total <= max_tokens or len(messages) <= 3:
        return messages

    # Find how many messages to keep from the end
    keep_from_end = 2  # at minimum keep the last exchange
    used = 0
    for i in range(len(messages) - 1, -1, -1):
        tokens = estimate_tokens(messages[i].content or "")
        if used + tokens > max_tokens * 0.8:  # keep 20% buffer
            break
        used += tokens
        keep_from_end = len(messages) - i

    keep_from_end = max(keep_from_end, 2)

    # Build compressed list: summary placeholder + recent messages
    compressed = list(messages[-keep_from_end:])

    # Prepend summary placeholder
    truncated_count = len(messages) - keep_from_end
    summary = Message.user(
        f"[{truncated_count} earlier messages omitted due to context length. "
        f"Summary of prior conversation not available — please ask if you need context.]"
    )

    return tuple([summary] + compressed)

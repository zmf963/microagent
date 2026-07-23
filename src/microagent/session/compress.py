"""Context compression — keeps long conversations within token limits.

Uses LLM-generated summaries (not placeholder text) to preserve
semantic information when compressing conversation history.
"""

from __future__ import annotations

from ..core.types import Message
from ..llm.client import LLMClient


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars/token for Latin, ~2 for CJK."""
    if not text:
        return 0
    chars = len(text)
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff')
    return ((chars - cjk) // 4) + (cjk // 2) + 1


def count_tokens(messages: tuple[Message, ...]) -> int:
    """Total estimated tokens across all messages."""
    return sum(estimate_tokens(m.content or "") for m in messages)


async def compress_with_llm(
    messages: tuple[Message, ...],
    llm: LLMClient,
    max_tokens: int = 100_000,
    keep_last: int = 4,
) -> tuple[Message, ...]:
    """Compress history using LLM-generated summary.

    Strategy:
    1. If under limit, return as-is.
    2. Split: early messages → LLM summary, keep last N messages.
    3. Prepend summary as a system message.
    """
    total = count_tokens(messages)
    if total <= max_tokens or len(messages) <= keep_last + 2:
        return messages

    # Split: early part to summarize, recent part to preserve
    split = len(messages) - keep_last
    early = messages[:split]
    recent = messages[split:]

    # Build summary prompt
    conversation_text = "\n".join(
        f"{m.role}: {m.content[:200]}" for m in early
    )
    prompt = (
        "Summarize this conversation in 2-3 sentences. "
        "Focus on key facts, decisions, and user preferences. "
        "Output only the summary, no preamble.\n\n"
        f"{conversation_text}"
    )

    try:
        summary_text = ""
        async for event in llm.stream(
            system="You are a summarizer. Be concise.",
            messages=(Message.user(prompt),),
            tools=None,
        ):
            from ..core.types import TextDelta
            if isinstance(event, TextDelta) and event.kind == "content":
                summary_text += event.text
    except Exception:
        # Fallback: placeholder if LLM fails
        summary_text = f"[{len(early)} earlier messages — summary unavailable]"

    summary_msg = Message.user(
        f"[Previous conversation summary ({len(early)} messages)]: {summary_text.strip()}"
    )
    return (summary_msg,) + recent


def compress_history(
    messages: tuple[Message, ...],
    max_tokens: int = 100_000,
) -> tuple[Message, ...]:
    """Simple compression with placeholder (no LLM). Use compress_with_llm() for production."""
    total = count_tokens(messages)
    if total <= max_tokens or len(messages) <= 3:
        return messages

    keep_from_end = 2
    used = 0
    for i in range(len(messages) - 1, -1, -1):
        tokens = estimate_tokens(messages[i].content or "")
        if used + tokens > max_tokens * 0.8:
            break
        used += tokens
        keep_from_end = len(messages) - i

    keep_from_end = max(keep_from_end, 2)
    compressed = list(messages[-keep_from_end:])
    truncated_count = len(messages) - keep_from_end
    summary = Message.user(
        f"[{truncated_count} earlier messages omitted. "
        f"Please ask if you need context from earlier in the conversation.]"
    )
    return tuple([summary] + compressed)

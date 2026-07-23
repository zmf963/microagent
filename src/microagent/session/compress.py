"""4-layer context compression pyramid for MicroAgent.

Inspired by Claude Code's 5-layer pyramid + Hermes structured summaries.

Layer 1 — Micro-Compact (zero API cost):
    Truncate re-obtainable tool results (>500 chars → placeholder).
    Trigger: messages > 50 or tokens > 60% of window.

Layer 2 — Snip (zero API cost):
    Remove oldest tool_result messages, keeping semantic messages intact.
    Trigger: Layer 1 insufficient.

Layer 3 — Structured LLM Summary (one API call):
    Generate 7-section structured summary + file attachment recovery.
    Trigger: Layer 2 insufficient. Threshold = window - AUTOCOMPACT_BUFFER.

Layer 4 — Circuit Breaker:
    After 3 consecutive failures, stop compacting (300s cooldown).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.types import Message


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTOCOMPACT_BUFFER = 8_000       # p99 summary output length
MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_SECONDS = 300

# Tool types whose results can be re-obtained
REOBTAINABLE_TOOLS = frozenset({
    "read_file", "grep", "glob", "bash",
    "web_fetch", "web_search", "context7",
    "browser_snapshot",
})

TRUNCATION_THRESHOLD = 500  # chars — truncate results longer than this
TRUNCATION_PLACEHOLDER = "[Tool result truncated: {n} chars — re-run the tool to get full output]"


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chars = len(text)
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff')
    return ((chars - cjk) // 4) + (cjk // 2) + 1


def count_tokens(messages: tuple[Message, ...]) -> int:
    return sum(estimate_tokens(m.content or "") for m in messages)


# ---------------------------------------------------------------------------
# Layer 1: Micro-Compact — truncate re-obtainable tool results
# ---------------------------------------------------------------------------

def micro_compact(
    messages: tuple[Message, ...],
    threshold: int = TRUNCATION_THRESHOLD,
) -> tuple[Message, ...]:
    """Zero-cost preprocessing: truncate long re-obtainable tool results.

    - Tool results from read_file/grep/bash etc. > threshold → placeholder
    - User messages are never truncated
    - Error messages are never truncated
    """
    result = list(messages)
    for i, msg in enumerate(result):
        if msg.role != "tool":
            continue
        if msg.is_error:
            continue  # preserve errors
        if len(msg.content) <= threshold:
            continue
        # Truncate: keep first 100 + last 100 chars for context
        truncated = (
            msg.content[:100]
            + f"\n\n{TRUNCATION_PLACEHOLDER.format(n=len(msg.content))}\n\n"
            + msg.content[-100:]
        )
        result[i] = Message(role="tool", content=truncated, tool_call_id=msg.tool_call_id)
    return tuple(result)


# ---------------------------------------------------------------------------
# Layer 2: Snip — remove oldest tool_result messages
# ---------------------------------------------------------------------------

def snip_tool_results(
    messages: tuple[Message, ...],
    keep_recent: int = 10,
    max_tokens: int = 200_000,
) -> tuple[Message, ...]:
    """Zero-cost: remove oldest tool_result messages until under token limit.

    - Preserves all user and assistant messages
    - Preserves the most recent `keep_recent` messages (any role)
    - Removes oldest tool_result messages first
    """
    if count_tokens(messages) <= max_tokens:
        return messages

    result = list(messages)
    # Protected zone: last `keep_recent` messages
    protected = set(range(max(0, len(result) - keep_recent), len(result)))

    # Remove oldest tool_result messages outside protected zone
    i = 0
    while i < len(result) and count_tokens(tuple(result)) > max_tokens:
        if i in protected:
            i += 1
            continue
        if result[i].role == "tool":
            result.pop(i)
            # Adjust protected indices
            protected = {p - 1 if p > i else p for p in protected}
        else:
            i += 1

    return tuple(result)


# ---------------------------------------------------------------------------
# Layer 3: Structured LLM Summary
# ---------------------------------------------------------------------------

COMPACTION_PROMPT_TEMPLATE = """You are compressing a conversation history for context recovery.

CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
Your entire response must be structured text.

Generate a summary using these 7 sections. Be specific — include file paths,
function names, error messages, and exact user quotes.

<analysis>
[Your internal reasoning about what is important — this will be stripped]
</analysis>
<summary>

## 1. 主要请求和意图
[The user's primary goal and any direction changes]

## 2. 关键技术决策
[Decisions made, libraries chosen, architecture choices, and WHY]

## 3. 涉及的文件和代码
[Files read/modified/created with paths and brief notes]

## 4. 遇到的错误和修复
[Errors encountered, how they were fixed, exact error messages if available]

## 5. 所有用户消息
[ENUMERATE every user message — do not summarize. Each one may be a direction change:
{user_messages}]

## 6. 待办任务
[Tasks not yet completed]

## 7. 当前进度
[What was being done when compaction fired — be specific to file and function level]

</summary>"""


def build_compaction_summary_prompt(
    messages: tuple[Message, ...],
) -> str:
    """Build the structured summary prompt with all user messages enumerated."""
    user_messages = "\n".join(
        f"- [{m.role}] {m.content[:200]}"
        for m in messages
        if m.role == "user"
    )
    if not user_messages:
        user_messages = "(no user messages)"

    return COMPACTION_PROMPT_TEMPLATE.format(user_messages=user_messages)


# ---------------------------------------------------------------------------
# Layer 4: CompactionState — circuit breaker + recursion guard
# ---------------------------------------------------------------------------

@dataclass
class CompactionState:
    """Tracks compaction failures and cooling-off periods."""

    consecutive_failures: int = 0
    _cooldown_until: float = 0.0
    _is_compaction_call: bool = False  # recursion guard

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self._cooldown_until = 0.0

    def activate_cooldown(self) -> None:
        self._cooldown_until = time.monotonic() + COOLDOWN_SECONDS

    def is_cooling_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def is_circuit_broken(self) -> bool:
        return self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES


# ---------------------------------------------------------------------------
# Main compression pipeline
# ---------------------------------------------------------------------------

async def compact_conversation(
    messages: tuple[Message, ...],
    llm: object,  # LLMClient
    context_window: int = 200_000,
    state: CompactionState | None = None,
) -> tuple[Message, ...]:
    """Run the 4-layer compression pipeline.

    Returns compressed messages. If all layers fail, returns placeholder.
    """
    if state is None:
        state = CompactionState()

    # Recursion guard: compaction calls must not trigger more compaction
    if state._is_compaction_call:
        return messages
    state._is_compaction_call = True

    try:
        # Circuit breaker
        if state.is_circuit_broken() or state.is_cooling_down():
            return _fallback(messages)

        current = messages

        # Layer 1: Micro-Compact (zero cost)
        layer1_threshold = int(context_window * 0.6)
        if count_tokens(current) > layer1_threshold:
            current = micro_compact(current)

        # Layer 2: Snip (zero cost)
        layer2_threshold = int(context_window * 0.8)
        if count_tokens(current) > layer2_threshold:
            current = snip_tool_results(current, max_tokens=layer2_threshold)

        # Layer 3: LLM Summary
        layer3_threshold = context_window - AUTOCOMPACT_BUFFER
        if count_tokens(current) > layer3_threshold:
            try:
                current = await _llm_summarize(current, llm)
                state.record_success()
            except Exception:
                state.record_failure()
                if state.is_circuit_broken():
                    state.activate_cooldown()
                return _fallback(current)

        return current
    finally:
        state._is_compaction_call = False


async def _llm_summarize(
    messages: tuple[Message, ...],
    llm: object,
) -> tuple[Message, ...]:
    """Generate structured LLM summary + return compressed messages."""
    prompt = build_compaction_summary_prompt(messages)

    summary_text = ""
    async for event in llm.stream(
        system="You are a context compressor. Be thorough and specific.",
        messages=(Message.user(prompt),),
        tools=None,
    ):
        from ..core.types import TextDelta
        if isinstance(event, TextDelta) and event.kind == "content":
            summary_text += event.text

    if not summary_text.strip():
        raise ValueError("LLM returned empty summary")

    # Strip <analysis> block, keep <summary>
    import re
    summary_only = re.sub(r'<analysis>.*?</analysis>', '', summary_text, flags=re.DOTALL).strip()

    # Build compressed result: summary message
    preamble = (
        "本会话是从之前一次因上下文耗尽而中断的对话延续过来的。"
        "以下摘要概述了之前的对话内容。请直接继续工作，不要重新询问已解决的问题。\n\n"
    )
    summary_msg = Message.user(preamble + summary_only)

    return (summary_msg,)


def _fallback(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """Circuit breaker fallback: placeholder text."""
    total = count_tokens(messages)
    placeholder = Message.user(
        f"[上下文压缩暂停：{len(messages)} 条消息，约 {total} tokens。"
        f"请基于最近的对话继续工作。如需早期上下文，请重新描述需求。]"
    )
    # Keep last 5 messages for continuity
    return (placeholder,) + messages[-5:]

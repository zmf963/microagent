"""5-layer context compression pyramid for MicroAgent.

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

Layer 5 — Full Dump:
    Append raw content of critical files verbatim (last-resort safety net).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.types import Message, TextDelta, Usage

if TYPE_CHECKING:
    from .budget import Budget

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTOCOMPACT_BUFFER = 8_000  # p99 summary output length
MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_SECONDS = 300

# Tool types whose results can be re-obtained
REOBTAINABLE_TOOLS = frozenset(
    {
        "read_file",
        "grep",
        "glob",
        "bash",
        "web_fetch",
        "web_search",
        "context7",
        "browser_snapshot",
    }
)

TRUNCATION_THRESHOLD = 500  # chars — truncate results longer than this
TRUNCATION_PLACEHOLDER = "[Tool result truncated: {n} chars — re-run the tool to get full output]"


# ---------------------------------------------------------------------------
# Heuristic tool result summaries (zero API cost)
# ---------------------------------------------------------------------------


def _is_grep_match_line(line: str) -> bool:
    """Check if a line looks like a grep -n match result.

    Standard grep -n format: ``path:line_number:text`` or ``line_number:text``.
    Returns True when the line contains at least one colon in the first
    80 characters (covers typical paths + line numbers) — not every
    colon-bearing string.
    """
    stripped = line.strip()
    if not stripped:
        return False
    # grep output: colon appears early (path:42:text or 42:text)
    first_colon = stripped.find(":")
    return 0 < first_colon < 80


def _summarize_tool_result(tool_name: str, content: str) -> str:
    """Generate a 1-line informative summary for a re-obtainable tool result.

    Instead of pure truncation, produces a tool-specific summary that
    preserves key information (exit codes, line counts, match counts).
    """
    lines = content.split("\n") if content else []
    n_lines = len(lines)

    if tool_name == "bash":
        # Try to detect exit code from common patterns
        exit_code = None
        for line in lines[-5:]:
            lowered = line.strip().lower()
            if lowered.startswith("exit code:") or lowered.startswith("exit:"):
                parts = lowered.split(":")
                if len(parts) >= 2:
                    try:
                        exit_code = int(parts[-1].strip())
                    except ValueError:
                        pass
        if exit_code is not None:
            return f"[bash] exit:{exit_code}, {n_lines} lines output"
        return f"[bash] {n_lines} lines output"

    if tool_name == "read_file":
        return f"[read_file] {n_lines} lines"

    if tool_name == "grep":
        # grep -n output format: "file:line:content" or "line:content"
        # Count lines that match the standard grep-with-line-numbers format
        match_count = sum(
            1 for line in lines
            if line.strip() and _is_grep_match_line(line)
        )
        return f"[grep] {match_count} matches"

    if tool_name == "glob":
        return f"[glob] {n_lines} entries"

    if tool_name == "web_fetch":
        return f"[web_fetch] {len(content)} chars"

    if tool_name == "web_search":
        return f"[web_search] {n_lines} results"

    if tool_name == "context7":
        return f"[context7] {len(content)} chars"

    if tool_name == "browser_snapshot":
        return f"[browser_snapshot] {len(content)} chars"

    # Generic fallback
    return TRUNCATION_PLACEHOLDER.format(n=len(content))


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Estimate token count: latin ~4 chars/token, CJK ~2 chars/token."""
    if not text:
        return 0
    chars = len(text)
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff")
    return ((chars - cjk) // 4) + (cjk // 2) + 1


def count_tokens(messages: tuple[Message, ...]) -> int:
    """Sum estimated tokens across all messages."""
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
    - Non-reobtainable tool results are preserved (may contain unique data)
    """
    # Build tool_call_id → tool_name mapping from assistant messages
    tc_names: dict[str, str] = {}
    for msg in messages:
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tc_names[tc.id] = tc.name

    result = list(messages)
    for i, msg in enumerate(result):
        if msg.role != "tool":
            continue
        if msg.is_error:
            continue  # preserve errors
        if len(msg.content) <= threshold:
            continue
        # Only truncate re-obtainable tool results (user can re-run the tool)
        tool_name = tc_names.get(msg.tool_call_id or "", "")
        if tool_name and tool_name not in REOBTAINABLE_TOOLS:
            continue  # preserve non-reobtainable results
        # Generate heuristic 1-line summary instead of raw truncation
        summary = _summarize_tool_result(tool_name, msg.content)
        result[i] = Message(role="tool", content=summary, tool_call_id=msg.tool_call_id)
    return tuple(result)


# ---------------------------------------------------------------------------
# Layer 2: Snip — remove oldest tool_result messages
# ---------------------------------------------------------------------------


def snip_tool_results(
    messages: tuple[Message, ...],
    keep_recent: int = 10,
    max_tokens: int = 200_000,
    protect_first_n: int = 3,
) -> tuple[Message, ...]:
    """Zero-cost: remove oldest tool_result messages until under token limit.

    - Preserves all user and assistant messages
    - Preserves the first `protect_first_n` messages (head protection)
    - Preserves the most recent `keep_recent` messages (tail protection)
    - Removes oldest tool_result messages outside protected zones first
    - **Tool-call integrity**: when a tool_result is removed, the matching
      tool_call id is stripped from the preceding assistant message. If that
      empties the assistant's tool_calls, a placeholder result is kept so the
      OpenAI API doesn't reject an orphaned tool_call (no matching tool msg).
    """
    total_tokens = count_tokens(messages)
    if total_tokens <= max_tokens:
        return messages

    result = list(messages)

    # Pre-compute per-message token counts to avoid O(n²) re-scanning
    msg_tokens = [estimate_tokens(m.content or "") for m in result]

    # Remove oldest tool_result messages outside protected zone
    i = 0
    while i < len(result) and total_tokens > max_tokens:
        # Recompute protected ranges each iteration — head/tail sizes
        # shift as messages are removed.
        head_protected = set(range(min(protect_first_n, len(result))))
        tail_protected = set(range(max(0, len(result) - keep_recent), len(result)))
        protected = head_protected | tail_protected

        if i in protected:
            i += 1
            continue
        if result[i].role == "tool":
            orphaned_id = result[i].tool_call_id
            total_tokens -= msg_tokens[i]
            result.pop(i)
            msg_tokens.pop(i)

            # Strip the matching tool_call from the preceding assistant message.
            if orphaned_id:
                _fix_orphaned_tool_call(result, orphaned_id)
        else:
            i += 1

    return tuple(result)


def _fix_orphaned_tool_call(
    messages: list[Message],
    orphaned_id: str,
) -> None:
    """Remove a tool_call id from its assistant message; keep API invariants.

    After snipping a tool_result, the matching tool_call in the assistant
    message is an orphan — the OpenAI API rejects tool_calls with no
    corresponding role=tool message.  We strip the id from the assistant's
    tool_calls tuple.  If that empties the tuple entirely, we cannot leave a
    bare assistant message with empty tool_calls either (some backends reject
    it); we replace it with a text-only assistant message carrying a note.
    """
    for i, msg in enumerate(messages):
        if not msg.tool_calls:
            continue
        if not any(tc.id == orphaned_id for tc in msg.tool_calls):
            continue
        new_calls = tuple(tc for tc in msg.tool_calls if tc.id != orphaned_id)
        if new_calls:
            messages[i] = Message(
                role=msg.role,
                content=msg.content,
                tool_calls=new_calls,
                tool_call_id=msg.tool_call_id,
                usage=msg.usage,
                is_error=msg.is_error,
            )
        else:
            # All tool_calls stripped — replace with a text note so the
            # assistant turn is still well-formed (no empty tool_calls list).
            messages[i] = Message.assistant(
                msg.content or "(earlier tool results were compacted away)"
            )
        break


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
        f"- [{m.role}] {m.content[:200]}" for m in messages if m.role == "user"
    )
    if not user_messages:
        user_messages = "(no user messages)"

    return COMPACTION_PROMPT_TEMPLATE.format(user_messages=user_messages)


INCREMENTAL_PROMPT_TEMPLATE = """You are updating an existing conversation summary with new context.

CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

You are given a PREVIOUS SUMMARY and NEW messages. Your job:
1. Preserve all important information from the previous summary
2. Add new facts, decisions, errors, and user messages from below
3. Update the "当前进度" section to reflect the latest state
4. Output a complete, up-to-date summary in the same 7-section format

=== PREVIOUS SUMMARY ===
{previous_summary}

=== NEW MESSAGES TO INCORPORATE ===
<analysis>
[Your internal reasoning — this will be stripped]
</analysis>
<summary>

## 1. 主要请求和意图
[Updated primary goal with any new direction changes]

## 2. 关键技术决策
[All decisions, old and new]

## 3. 涉及的文件和代码
[All files, old and new]

## 4. 遇到的错误和修复
[All errors, old and new]

## 5. 所有用户消息
[ENUMERATE every user message from the NEW messages:
{user_messages}]

## 6. 待办任务
[Tasks not yet completed]

## 7. 当前进度
[Latest state — what was being done when these new messages were added]

</summary>"""


def build_incremental_summary_prompt(
    messages: tuple[Message, ...],
    previous_summary: str,
) -> str:
    """Build prompt for updating an existing summary with new messages."""
    user_messages = "\n".join(
        f"- [{m.role}] {m.content[:200]}" for m in messages if m.role == "user"
    )
    if not user_messages:
        user_messages = "(no new user messages)"

    return INCREMENTAL_PROMPT_TEMPLATE.format(
        previous_summary=previous_summary,
        user_messages=user_messages,
    )


# ---------------------------------------------------------------------------
# Layer 4: CompactionState — circuit breaker + recursion guard
# ---------------------------------------------------------------------------


@dataclass
class CompactionState:
    """Tracks compaction failures, cooling-off, previous summary, and recursion."""

    consecutive_failures: int = 0
    previous_summary: str | None = None  # iterative summary across compactions
    _cooldown_until: float = 0.0
    _is_compaction_call: bool = False  # recursion guard
    _ineffective_count: int = 0  # anti-jitter: count consecutive ineffective compressions

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self._cooldown_until = 0.0
        self._ineffective_count = 0

    def record_ineffective(self) -> None:
        """Record a compression that saved <10% tokens."""
        self._ineffective_count += 1

    def should_skip_compression(self) -> bool:
        """After 2 ineffective compressions, skip until next user input."""
        return self._ineffective_count >= 2

    def reset_for_new_turn(self) -> None:
        """Reset anti-jitter counter on new user input."""
        self._ineffective_count = 0

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
    llm: object,
    context_window: int = 200_000,
    state: CompactionState | None = None,
    force: bool = False,
    budget: Budget | None = None,
    strict_role_alternation: bool = False,
) -> tuple[Message, ...]:
    """Run the 4-layer compression pipeline.

    force=True (manual /compact): skips L1+L2 thresholds, goes to LLM summary.
      Clears circuit breaker cooldown so manual retry works immediately.
    force=False (auto): runs full L1-L2-L3-L4 pipeline with thresholds.
    Uses state.previous_summary for incremental compaction when available.

    If budget is provided, LLM summary token usage is consumed against it.
    """
    if state is None:
        state = CompactionState()

    if state._is_compaction_call:
        return messages
    state._is_compaction_call = True

    try:
        from .budget import BudgetExceeded

        # Manual /compact: clear cooldown, skip to LLM
        if force:
            state._cooldown_until = 0.0
            try:
                prev = state.previous_summary
                current, usage = await _llm_summarize(messages, llm, previous_summary=prev)
                if budget is not None and usage:
                    await budget.consume(
                        tokens=usage.input_tokens + usage.output_tokens,
                        cost_usd=usage.cost_usd,
                    )
                # Recover recent file attachments after summary
                from .attachments import recover_file_attachments

                current = recover_file_attachments(messages, current)
                state.previous_summary = _extract_summary_text(current)
                state.record_success()
                return current
            except BudgetExceeded:
                raise
            except Exception:
                state.record_failure()
                if state.is_circuit_broken():
                    state.activate_cooldown()
                return _fallback(messages)

        # Auto: circuit breaker
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

        # Layer 3: LLM Summary (incremental if previous summary exists)
        layer3_threshold = context_window - AUTOCOMPACT_BUFFER
        if count_tokens(current) > layer3_threshold:
            try:
                prev = state.previous_summary
                current, usage = await _llm_summarize(current, llm, previous_summary=prev)
                if budget is not None and usage:
                    await budget.consume(
                        tokens=usage.input_tokens + usage.output_tokens,
                        cost_usd=usage.cost_usd,
                    )
                # Recover recent file attachments after summary
                from .attachments import recover_file_attachments

                current = recover_file_attachments(messages, current)
                state.previous_summary = _extract_summary_text(current)
                state.record_success()
            except BudgetExceeded:
                raise
            except Exception:
                state.record_failure()
                if state.is_circuit_broken():
                    state.activate_cooldown()
                return _fallback(current)

        return _ensure_role_alternation(current, strict=strict_role_alternation)
    finally:
        state._is_compaction_call = False


def _extract_summary_text(compressed: tuple[Message, ...]) -> str:
    """Extract the summary text from compressed messages for iterative storage.

    Relies on _llm_summarize always returning a single-user-message tuple.
    """
    if compressed and compressed[0].role == "user":
        return compressed[0].content
    return ""


async def _llm_summarize(
    messages: tuple[Message, ...],
    llm: object,
    previous_summary: str | None = None,
) -> tuple[tuple[Message, ...], Usage | None]:
    """Generate structured LLM summary + return compressed messages and usage.

    If previous_summary is provided, uses incremental update mode.
    Returns (compressed_messages, usage) where usage may be None if the
    LLM stream did not include token counts.
    """
    if previous_summary:
        prompt = build_incremental_summary_prompt(messages, previous_summary)
    else:
        prompt = build_compaction_summary_prompt(messages)

    summary_text = ""
    usage: Usage | None = None
    async for event in llm.stream(
        system="You are a context compressor. Be thorough and specific.",
        messages=(Message.user(prompt),),
        tools=None,
    ):
        if isinstance(event, TextDelta) and event.kind == "content":
            summary_text += event.text
        elif isinstance(event, Usage):
            usage = event

    if not summary_text.strip():
        raise ValueError("LLM returned empty summary")

    # Strip <analysis> block, keep <summary>
    summary_only = re.sub(r"<analysis>.*?</analysis>", "", summary_text, flags=re.DOTALL).strip()

    # Build compressed result: summary message
    preamble = (
        "本会话是从之前一次因上下文耗尽而中断的对话延续过来的。"
        "以下摘要概述了之前的对话内容。请直接继续工作，不要重新询问已解决的问题。\n\n"
    )
    summary_msg = Message.user(preamble + summary_only)

    return (summary_msg,), usage


def _fallback(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """Circuit breaker fallback: placeholder text."""
    total = count_tokens(messages)
    placeholder = Message.user(
        f"[上下文压缩暂停：{len(messages)} 条消息，约 {total} tokens。"
        f"请基于最近的对话继续工作。如需早期上下文，请重新描述需求。]"
    )
    # Keep last 5 messages for continuity
    return (placeholder,) + messages[-5:]


def _ensure_role_alternation(
    messages: tuple[Message, ...],
    *,
    strict: bool = False,
) -> tuple[Message, ...]:
    """Ensure no adjacent same-role messages (user/assistant).

    When strict=False, return messages unchanged.
    When strict=True, insert empty messages between adjacent same-role pairs.
    Tool messages are skipped (they follow tool_call pairing rules).
    """
    if not strict:
        return messages

    result: list[Message] = []
    prev_role: str | None = None
    for msg in messages:
        if msg.role == "tool":
            result.append(msg)
            continue
        if prev_role is not None and msg.role == prev_role:
            # Insert empty message of the opposite role
            if msg.role == "user":
                result.append(Message.assistant(""))
            else:
                result.append(Message.user(""))
        result.append(msg)
        prev_role = msg.role
    return tuple(result)


# ---------------------------------------------------------------------------
# Layer 5: Full Dump — append critical raw file content verbatim
# ---------------------------------------------------------------------------

LAYER5_MAX_FILES = 3
LAYER5_MAX_CHARS = 8000  # per file


def layer5_full_dump(
    messages: tuple[Message, ...],
) -> tuple[Message, ...]:
    """Layer 5 — append raw content of critical files as a fallback dump.

    When L1-L4 compression still leaves insufficient context, this layer
    reads the most recently referenced files and appends their raw content.
    This mirrors Claude Code's L5 "full dump" — a high-token-cost safety
    net that preserves file context when all else fails.
    """
    from .attachments import _extract_file_paths

    files = _extract_file_paths(messages)
    if not files:
        return messages

    parts: list[str] = []
    count = 0
    for fpath in list(files)[:LAYER5_MAX_FILES]:
        try:
            content = Path(fpath).expanduser().read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if len(content) > LAYER5_MAX_CHARS:
            content = content[:LAYER5_MAX_CHARS] + "\n...[truncated]..."
        parts.append(f"=== {fpath} ===\n{content}")
        count += 1

    if not parts:
        return messages

    dump_msg = Message.user(f"[L5 Full Dump — {count} critical file(s)]\n\n" + "\n\n".join(parts))
    return messages + (dump_msg,)

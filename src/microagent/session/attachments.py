"""File attachment recovery — restores recent file contents after compaction.

When the 4-layer compression pyramid fires L3 (LLM summary), the conversation
history is distilled into a 7-section summary. File contents that were
originally read by tools are lost in the process.

This module recovers the most recently accessed files and appends their
content as attachments to the compressed messages, so the agent retains
file-level context without re-reading from disk.

Limits (matching Claude Code design):
  - Max 3 files
  - Max 3000 chars per file
  - Only files accessed in the original (pre-compression) messages
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.types import Message

# Limits
MAX_FILES = 3
MAX_CHARS_PER_FILE = 3000
_MAX_PREVIEW_CHARS = 500  # first 500 chars per file in prompt


def _extract_file_paths(messages: tuple[Message, ...]) -> dict[str, int]:
    """Extract file paths mentioned in tool calls and their last occurrence index.

    Returns dict of {path: last_seen_index}, sorted by recency.
    """
    paths: dict[str, int] = {}

    # Match file paths from tool call arguments (structured, reliable).
    # Fall back to content scanning only for assistant/user messages that
    # explicitly mention file paths.
    for i, msg in enumerate(messages):
        # Structured tool calls — the primary source
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.name in ("read_file", "write_file", "edit_file", "grep", "glob", "bash"):
                    for arg_val in tc.arguments.values():
                        if isinstance(arg_val, str):
                            for candidate in _parse_paths_from_string(arg_val):
                                paths[candidate] = i

        # Content-based fallback — scan message text for explicit path mentions
        if msg.content:
            for candidate in _parse_paths_from_string(msg.content):
                paths[candidate] = max(paths.get(candidate, 0), i)

    # Sort by recency (last seen first), take top MAX_FILES
    sorted_paths = sorted(paths.items(), key=lambda x: -x[1])
    return dict(sorted_paths[:MAX_FILES])


def _parse_paths_from_string(text: str) -> list[str]:
    """Extract plausible file paths from a string.

    Uses a character-class approach: a path is a contiguous run of
    non-whitespace, non-punctuation characters that starts with ``/``,
    ``./``, ``~/``, a drive letter, or an alpha-numeric identifier
    containing a dot (extension).  Returns deduplicated list.
    """
    if not text:
        return []

    import re

    # Anchored paths: /foo/bar, ./foo/bar, ~/foo/bar, C:\foo\bar,
    # and unqualified relative paths: src/main.py, lib/util.go
    anchored = re.compile(
        r'(?:/|\.\.?/|~/|[A-Za-z]:[/\\]|[A-Za-z0-9_][\w./+-]*/)'
        r'[\w./+-]{0,200}'
        r'(?:\.\w{1,15})?'  # optional extension
    )
    # Bare filenames with extensions: foo.py, my_file.txt
    filename = re.compile(
        r'\b[\w.-]{1,200}\.\w{1,15}\b'
    )

    seen: set[str] = set()
    result: list[str] = []

    for pattern in (anchored, filename):
        for m in pattern.finditer(text):
            p = m.group(0).rstrip("./\\")
            if not p or p in seen:
                continue
            if _is_readable_file(p):
                seen.add(p)
                result.append(p)

    return result


def _is_readable_file(path: str) -> bool:
    """Check if a path string looks like a readable file."""
    if not path or len(path) > 500:
        return False
    # Exclude obvious non-files
    if path.endswith(("/", "\\", ":", ">", "<", "|", "&", ";")):
        return False
    if "://" in path:
        return False
    # Has extension, or is a known extensionless file
    filename = path.split("/")[-1]
    if "." in filename:
        return True
    if filename in (
        "Makefile",
        "Dockerfile",
        "AGENTS",
        "CLAUDE",
        "Gemfile",
        "Rakefile",
        "CMakeLists",
    ):
        return True
    # Absolute paths with no extension but that look like files
    if filename and not filename.endswith("/"):
        if path.startswith("/") and len(filename) < 60:
            # Skip binary/system paths
            if any(
                p in path
                for p in ("/bin/", "/sbin/", "/usr/lib/", "/usr/share/", "/dev/", "/proc/")
            ):
                return False
            return True
    return False


def recover_file_attachments(
    messages: tuple[Message, ...],
    compressed: tuple[Message, ...],
) -> tuple[Message, ...]:
    """Append file content attachments to compressed messages.

    Extracts file paths from the original messages (pre-compression),
    reads the most recent files, and appends their content as
    attachment messages to the compressed result.
    """
    if not messages:
        return compressed

    files = _extract_file_paths(messages)
    if not files:
        return compressed

    attachments = []
    for fpath in files:
        try:
            content = Path(fpath).expanduser().read_text()
        except (OSError, UnicodeDecodeError):
            continue

        if len(content) > MAX_CHARS_PER_FILE:
            content = content[:MAX_CHARS_PER_FILE] + "\n...[truncated]..."

        attachment = (
            f"[已恢复文件: {fpath}]\n"
            f"(该文件是压缩前被操作的。内容上限 {MAX_CHARS_PER_FILE} 字符)\n\n"
            f"{content}"
        )
        attachments.append(attachment)

    if not attachments:
        return compressed

    # Append attachments as a single message after the summary
    attachment_msg = Message.user(
        f"[上下文压缩附件 — {len(attachments)} 个文件恢复]\n\n" + "\n\n---\n\n".join(attachments)
    )

    return compressed + (attachment_msg,)

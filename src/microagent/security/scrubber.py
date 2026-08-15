"""StreamingContextScrubber — strips <context> fence from streaming output.

Prevents the LLM from echoing injected context fence content back to the user.
Stateful: handles fence content split across multiple feed() calls.

Tags are matched case-insensitively with optional whitespace/attributes —
an LLM echoing the fence as ``<Context data-x=1>`` or ``</context >``
previously sailed straight through, and a literal ``</Context>`` inside
ordinary prose truncated legitimate output.

**Library extension point — NOT applied by the CLI by default.** The
surface CLI emits TextDelta events directly to Rich without scrubbing
``<context>...</context>`` fences. Library users streaming TextDelta
events to untrusted viewers should wrap their consumer:
``safe = scrubber.feed(chunk)`` before display/forwarding.
"""

from __future__ import annotations

import re

_OPEN_TAG = "<context>"
_CLOSE_TAG = "</context>"

# Tag shape with case-insensitivity + optional whitespace/attributes.
# Open: <context> <CONTEXT> <context x=1> <context > <context/>
# Close: </context> </Context> </context >  (no attributes on closers —
# HTML forbids them; being lax here would let '</context foo>' pass)
_OPEN_RE = re.compile(r"<context\b[^>]*>", re.IGNORECASE)
_CLOSE_RE = re.compile(r"</context\s*>", re.IGNORECASE)


class StreamingContextScrubber:
    """Strips <context>...</context> spans from streaming text.

    Maintains a buffer to handle tags split across feed() calls.
    When inside a fence span, all content is discarded.
    flush() at end of stream discards any pending in-span content.

    Nested opening tags inside a span are tolerated: the span ends at the
    FIRST close tag. Content between the nested open and the first close
    is discarded (it lives inside the fence); content after the first
    close is emitted normally.
    """

    def __init__(self):
        self._buffer = ""
        self._in_span = False

    def feed(self, chunk: str) -> str:
        """Process a chunk of streaming text. Returns safe text to display."""
        self._buffer += chunk
        output: list[str] = []

        while self._buffer:
            if self._in_span:
                m = _CLOSE_RE.search(self._buffer)
                if m is None:
                    # Still inside span — discard everything; keep the
                    # tail (longest partial close tag) for split-tag
                    # matching across feed() calls.
                    keep = self._longest_partial(_CLOSE_RE, self._buffer)
                    self._buffer = self._buffer[-keep:]
                    break
                # First close ends the span — everything up to and
                # including it is discarded.
                self._buffer = self._buffer[m.end():]
                self._in_span = False
                continue
            else:
                m = _OPEN_RE.search(self._buffer)
                if m is None:
                    partial = self._check_partial_open()
                    if partial:
                        output.append(self._buffer[:-partial])
                        self._buffer = self._buffer[-partial:]
                    else:
                        output.append(self._buffer)
                        self._buffer = ""
                    break
                # Found opening tag — output everything before it.
                # A bare '<' before the match (e.g. text ended with '<'
                # in a previous chunk) must not leak the raw '<' — emit
                # only through the position where the tag actually
                # starts.
                output.append(self._buffer[:m.start()])
                self._buffer = self._buffer[m.end():]
                self._in_span = True
                continue

        return "".join(output)

    def _check_partial_open(self) -> int:
        """Check if buffer ends with a partial opening tag. Returns partial length."""
        text = self._buffer
        for length in range(min(len(text), len(_OPEN_TAG) - 1), 0, -1):
            if _OPEN_TAG.lower().startswith(text[-length:].lower()):
                return length
        return 0

    @staticmethod
    def _longest_partial(close_re: re.Pattern, text: str) -> int:
        """Longest suffix of text that is a prefix of any close-tag match.

        Keeps split </context> (or </Context>) fragments across feed()
        calls without holding unbounded in-span content.
        """
        best = 0
        for end in range(1, min(len(text), len(_CLOSE_TAG)) + 1):
            suffix = text[-end:].lower()
            if _CLOSE_TAG.lower().startswith(suffix) or suffix in ("<", "</"):
                best = end
        return best

    def flush(self) -> str:
        """Called at end of stream. Returns any remaining safe content.

        If still inside a span, discards the pending content.
        """
        if self._in_span:
            # Discard everything — incomplete span
            result = ""
        else:
            result = self._buffer
        self._buffer = ""
        self._in_span = False
        return result

    def reset(self) -> None:
        """Reset scrubber state for reuse."""
        self._buffer = ""
        self._in_span = False

"""StreamingContextScrubber — strips <context> fence from streaming output.

Prevents the LLM from echoing injected context fence content back to the user.
Stateful: handles fence content split across multiple feed() calls.
"""

from __future__ import annotations

import re

_OPEN_TAG = "<context>"
_CLOSE_TAG = "</context>"


class StreamingContextScrubber:
    """Strips <context>...</context> spans from streaming text.

    Maintains a buffer to handle tags split across feed() calls.
    When inside a fence span, all content is discarded.
    flush() at end of stream discards any pending in-span content.
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
                # Look for closing tag
                close_idx = self._buffer.find(_CLOSE_TAG)
                if close_idx == -1:
                    # Still inside span — discard everything, keep buffer for tag matching
                    # Keep last len(close_tag)-1 chars in case tag is split
                    keep = len(_CLOSE_TAG) - 1
                    if len(self._buffer) > keep:
                        self._buffer = self._buffer[-keep:]
                    break
                else:
                    # Found closing tag — discard everything up to and including it
                    self._buffer = self._buffer[close_idx + len(_CLOSE_TAG):]
                    self._in_span = False
                    continue
            else:
                # Look for opening tag
                open_idx = self._buffer.find(_OPEN_TAG)
                if open_idx == -1:
                    # No opening tag — but check for partial match at end
                    partial = self._check_partial_open()
                    if partial:
                        output.append(self._buffer[:-partial])
                        self._buffer = self._buffer[-partial:]
                    else:
                        output.append(self._buffer)
                        self._buffer = ""
                    break
                else:
                    # Found opening tag — output everything before it
                    output.append(self._buffer[:open_idx])
                    self._buffer = self._buffer[open_idx + len(_OPEN_TAG):]
                    self._in_span = True
                    continue

        return "".join(output)

    def _check_partial_open(self) -> int:
        """Check if buffer ends with a partial <context> tag. Returns partial length."""
        for length in range(min(len(self._buffer), len(_OPEN_TAG) - 1), 0, -1):
            if _OPEN_TAG.startswith(self._buffer[-length:]):
                return length
        return 0

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

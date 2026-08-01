"""Shared security helpers for builtin tools.

These helpers harden tools that accept ids / names from the LLM (which can
be prompt-injected) and join them into filesystem paths. Without sanitization,
ids like '../../etc/cron.d/evil' would let a tool write outside its intended
directory (path traversal → arbitrary file write / delete).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# Safe id chars: alphanumerics, underscore, dash, dot. Rejects path
# separators, '..', and anything that could escape a directory.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def is_safe_name(name: str) -> bool:
    """Return True if name is safe to embed in a path component.

    A safe name starts with an alphanumeric character and contains only
    alphanumerics, underscore, dash, or dot. This rejects '../', absolute
    paths, separators, and empty strings.
    """
    if not name or len(name) > 255:
        return False
    # Reject dot-only names ('.', '..') and any segment containing '..'
    # even if the regex would otherwise allow 'a..b'.
    if ".." in name:
        return False
    return bool(_SAFE_NAME_RE.match(name))


def safe_filename_from_id(id_str: str) -> str:
    """Convert an arbitrary id into a safe, collision-resistant filename.

    Use this when the id is opaque (e.g. tool_call_id, session_id) and you
    don't need it to be human-readable — SHA-256 makes traversal and
    collisions computationally infeasible.
    """
    return hashlib.sha256(id_str.encode("utf-8")).hexdigest()[:32]


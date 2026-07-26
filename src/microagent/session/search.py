"""session_search — FTS5 full-text search across all past sessions.

Uses SQLite FTS5 with BM25 ranking for fast, ranked full-text search.
FTS5 index is maintained automatically via triggers on the messages table.
"""

from __future__ import annotations

import json
import re

from ..core.store import SQLiteStore, Store
from ..core.types import Message

# ---------------------------------------------------------------------------
# FTS5 schema helpers
# ---------------------------------------------------------------------------

FTS5_INIT_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    role,
    content,
    session_id,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, role, content, session_id)
    VALUES (new.id, json_extract(new.data, '$.role'),
            json_extract(new.data, '$.content'),
            new.session_id);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, role, content, session_id)
    VALUES ('delete', old.id, json_extract(old.data, '$.role'),
            json_extract(old.data, '$.content'),
            old.session_id);
END;
"""


def ensure_fts5(store: SQLiteStore) -> None:
    """Ensure FTS5 index exists on the store (idempotent)."""
    conn = store._conn
    try:
        conn.executescript(FTS5_INIT_SQL)
    except Exception:
        pass  # FTS5 may already exist or be unsupported


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

# CJK character ranges
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]+")
_LATIN_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def _build_fts_query(query: str) -> str:
    """Build an FTS5 query string with CJK-aware tokenization.

    Latin words are quoted for phrase matching.
    CJK text is split into bigrams (e.g. "代码审查" → "代码 码审 审查")
    for better precision than single-character OR.
    FTS5 special characters are stripped to prevent syntax errors.
    """
    # Strip FTS5 special characters that could cause syntax errors
    safe_query = query.translate(str.maketrans("", "", '*"()^'))

    parts = []
    pos = 0
    for m in _CJK_RE.finditer(safe_query):
        # Add Latin text before this CJK segment
        latin = safe_query[pos : m.start()].strip()
        if latin:
            words = _LATIN_WORD_RE.findall(latin)
            parts.extend(f'"{w}"' for w in words)

        # Split CJK into bigrams
        cjk = m.group()
        bigrams = [cjk[i : i + 2] for i in range(len(cjk) - 1)]
        if bigrams:
            parts.extend(bigrams)
        else:
            # Single CJK character — keep as-is
            parts.append(cjk)
        pos = m.end()

    # Remaining Latin text
    remaining = safe_query[pos:].strip()
    if remaining:
        words = _LATIN_WORD_RE.findall(remaining)
        parts.extend(f'"{w}"' for w in words)

    return " OR ".join(parts) if parts else f'"{safe_query}"'


async def search_sessions(
    store: Store,
    query: str,
    k: int = 5,
) -> tuple[Message, ...]:
    """Search all stored sessions using FTS5 with BM25 ranking.

    Falls back to LIKE if FTS5 is not available.
    """
    if not isinstance(store, SQLiteStore):
        return ()

    ensure_fts5(store)
    fts_query = _build_fts_query(query)

    try:
        # FTS5 with ranking — higher rank = better match
        rows = store._conn.execute(
            """SELECT data, rank FROM messages_fts
               WHERE messages_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, k),
        ).fetchall()

        if not rows:
            return ()

        results = []
        for data, _rank in rows:
            try:
                obj = json.loads(data)
                results.append(
                    Message(
                        role=obj.get("role", "user"),
                        content=obj.get("content", ""),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        return tuple(results)

    except Exception:
        # FTS5 unavailable — fallback to LIKE
        safe_query = query.replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{safe_query}%"
        rows = store._conn.execute(
            "SELECT data FROM messages WHERE data LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
            (pattern, k),
        ).fetchall()

        results = []
        for (data,) in rows:
            try:
                obj = json.loads(data)
                results.append(
                    Message(
                        role=obj.get("role", "user"),
                        content=obj.get("content", ""),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        return tuple(results)

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
    session_id
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, role, content, session_id)
    VALUES (new.id, json_extract(new.data, '$.role'),
            json_extract(new.data, '$.content'),
            new.session_id);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;
"""


def _has_broken_fts_schema(conn) -> bool:
    """Detect the old external-content shape (content=messages).

    That schema never worked: FTS5 columns role/content/session_id do not
    exist as real columns on the messages table (the values live inside the
    JSON data blob), so every query raised 'no such column: T.role' and was
    silently swallowed by the except-fallback in _search. The index was
    created but unusable. Returns True if the table exists in that shape and
    must be migrated.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if not row or not row[0]:
        return False
    return "content=messages" in (row[0] or "").replace(" ", "")


def ensure_fts5(store: SQLiteStore) -> None:
    """Ensure the FTS5 index exists on the store (idempotent + self-healing).

    Migrates away from the broken external-content shape (content=messages)
    by dropping and rebuilding as a self-contained table, then backfills the
    index from any messages that pre-date the index. Idempotent: a correct
    table is left untouched.
    """
    conn = store._conn
    need_backfill = False
    if _has_broken_fts_schema(conn):
        conn.execute("DROP TRIGGER IF EXISTS messages_ai")
        conn.execute("DROP TRIGGER IF EXISTS messages_ad")
        conn.execute("DROP TABLE IF EXISTS messages_fts")
        need_backfill = True
    elif conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone() is None:
        need_backfill = True  # fresh table — index any pre-existing messages

    try:
        conn.executescript(FTS5_INIT_SQL)
    except Exception:
        pass  # FTS5 may be unsupported on this build

    if need_backfill:
        try:
            conn.execute(
                "INSERT INTO messages_fts(rowid, role, content, session_id) "
                "SELECT id, json_extract(data, '$.role'), "
                "json_extract(data, '$.content'), session_id FROM messages"
            )
        except Exception:
            pass


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

        # Split CJK into bigrams. FTS5's unicode61 tokenizer indexes a CJK
        # run as ONE token ("代码审查非常重要" is a single token — no CJK
        # segmentation), so a bare bigram like 代码 never matches. Prefix
        # matching (代码*) does: the indexed run starts with the bigram.
        # Without the '*', every CJK query silently returned 0 rows with no
        # error (so the LIKE fallback never fired) — verified against real
        # sqlite3. The '*' is added here after user input was stripped of
        # FTS5 specials, so it can't break the query grammar.
        cjk = m.group()
        bigrams = [cjk[i : i + 2] for i in range(len(cjk) - 1)]
        if bigrams:
            parts.extend(f"{bg}*" for bg in bigrams)
        else:
            # Single CJK character — keep as-is
            parts.append(f"{cjk}*")
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

    All SQLite I/O runs under ``store._lock`` via ``asyncio.to_thread`` —
    the same discipline as every SQLiteStore method. Previously this ran
    raw sync sqlite3 on the event loop thread: it blocked the loop for
    the duration of the query and raced with concurrent append() writes
    on the same connection.
    """
    if not isinstance(store, SQLiteStore):
        return ()

    import asyncio

    fts_query = _build_fts_query(query)

    def _rows_to_messages(rows) -> tuple[Message, ...]:
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

    def _search() -> tuple[Message, ...]:
        ensure_fts5(store)
        # CJK queries bypass FTS entirely: unicode61 indexes a CJK run as
        # ONE token with no segmentation, so FTS can only match run-initial
        # prefixes — '代码' never matches '用户的代码审查...'. LIKE substring
        # matching is correct for CJK; require every extracted term (latin
        # words + CJK runs) to appear (AND semantics).
        if _CJK_RE.search(query):
            terms = _LATIN_WORD_RE.findall(query) + _CJK_RE.findall(query)
            if terms:
                where = " AND ".join("data LIKE ? ESCAPE '\\'" for _ in terms)
                patterns = [
                    f"%{t.replace(chr(92), chr(92)*2).replace('%', chr(92)+'%').replace('_', chr(92)+'_')}%"
                    for t in terms
                ]
                rows = [
                    (data, None)
                    for (data,) in store._conn.execute(
                        f"SELECT data FROM messages WHERE {where} "
                        "ORDER BY id DESC LIMIT ?",
                        (*patterns, k),
                    ).fetchall()
                ]
                return _rows_to_messages(rows)
        try:
            # FTS5 with ranking — higher rank = better match. Join the FTS
            # rowid back to messages.id to recover the full JSON data (the
            # self-contained FTS table holds indexed text, not the raw blob).
            rows = store._conn.execute(
                """SELECT m.data, f.rank FROM messages_fts f
                   JOIN messages m ON m.id = f.rowid
                   WHERE messages_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, k),
            ).fetchall()
        except Exception:
            # FTS5 unavailable — fallback to LIKE
            safe_query = query.replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{safe_query}%"
            rows = [
                (data, None)
                for (data,) in store._conn.execute(
                    "SELECT data FROM messages WHERE data LIKE ? ESCAPE '\\' "
                    "ORDER BY id DESC LIMIT ?",
                    (pattern, k),
                ).fetchall()
            ]
        return _rows_to_messages(rows)

    async with store._lock:
        return await asyncio.to_thread(_search)

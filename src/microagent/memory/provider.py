"""Memory data model, MemoryProvider Protocol, and SQLiteMemoryProvider.

Cross-session memory is optional — no memory code runs unless a
MemoryProvider is configured. The default SQLite+FTS5 implementation
provides full-text search with zero external dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.types import Message

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Memory dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Memory:
    """A single memory entry."""

    id: str
    content: str
    category: str  # "fact" | "preference" | "task" | "context"
    created_at: float
    relevance_score: float = 0.0  # larger = more relevant (FTS5 bm25 negated)
    session_id: str | None = None  # None = cross-session
    visibility: str = "private"  # private | shared | redacted
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# MemoryProvider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryProvider(Protocol):
    """Protocol for pluggable memory backends."""

    async def prefetch(self, query: str) -> None:
        """Pre-warm the cache for a query (fire-and-forget)."""
        ...

    async def recall(self, query: str, k: int = 5) -> tuple[Memory, ...]:
        """Retrieve top-k relevant memories."""
        ...

    async def sync_turn(self, session_id: str, history: tuple[Message, ...]) -> None:
        """Store new memories from a completed turn."""
        ...

    async def batch_write(self, memories: tuple[Memory, ...]) -> None:
        """Write multiple memories at once."""
        ...

    async def delete(self, memory_id: str) -> None:
        """Remove a single memory."""
        ...

    async def pending_memories(self) -> tuple[Memory, ...]:
        """Memories held for approval (write_approval mode)."""
        ...

    async def approve_memory(self, memory_id: str) -> None:
        """Approve one pending memory into live storage."""
        ...

    async def reject_memory(self, memory_id: str) -> None:
        """Reject (discard) one pending memory."""
        ...

    def system_prompt_block(self) -> str:
        """Return a fixed block to inject into the system prompt."""
        ...


# ---------------------------------------------------------------------------
# SQLiteMemoryProvider — FTS5 full-text search, zero dependencies
# ---------------------------------------------------------------------------


class SQLiteMemoryProvider:
    """SQLite + FTS5 implementation of MemoryProvider."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        category TEXT NOT NULL,
        created_at REAL NOT NULL,
        session_id TEXT,
        visibility TEXT NOT NULL DEFAULT 'private',
        metadata TEXT,
        content_hash TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content, content='memories', content_rowid='rowid'
    );
    CREATE TABLE IF NOT EXISTS pending_memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        category TEXT NOT NULL,
        created_at REAL NOT NULL,
        session_id TEXT,
        visibility TEXT NOT NULL DEFAULT 'private',
        metadata TEXT,
        content_hash TEXT
    );
    """

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # busy_timeout so concurrent writers (CLI + agent, multiple agents)
        # wait instead of crashing with "database is locked". WAL mode is
        # enabled before any writes so the schema script runs in WAL.
        # check_same_thread=False: all access is serialized by self._lock
        # via asyncio.to_thread (same discipline as SQLiteStore), so the
        # connection legitimately crosses threads — never concurrently.
        self._conn = sqlite3.connect(str(self._path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self.SCHEMA)
        self._migrate_content_hash()
        # All DB work runs under this lock via asyncio.to_thread — the same
        # discipline as session/search.py + SQLiteStore. Previously every
        # public async method ran synchronous sqlite3 directly on the event
        # loop thread: recall() during a turn blocked streaming/tool calls,
        # and concurrent writers raced on the shared connection.
        self._lock = asyncio.Lock()
        # Hermes-style write-approval gate. False (default): batch_write
        # lands directly in memories (Hermes default write_approval: false).
        # True: batch_write holds entries in pending_memories until
        # approve_memory / reject_memory — the CLI's /memory command
        # drives the gate (Hermes: /memory [pending|approve|reject]).
        self.write_approval = False

    # Rolling size cap. Hermes keeps MEMORY.md bounded by a char limit and
    # asks the LLM to compact; the SQLite form uses a row cap and evicts
    # the oldest entries (context-category first — they are the least
    # durable, derived from raw conversation windows).
    MAX_MEMORIES = 500

    @staticmethod
    def _normalize_content(content: str) -> str:
        """Canonical form for dedupe: strip, collapse whitespace, lowercase.

        Deliberately conservative — punctuation and word order are kept,
        so genuine revisions are not false-positive duplicates.
        """
        return " ".join(content.split()).lower()

    @classmethod
    def _content_hash(cls, content: str) -> str:
        import hashlib

        return hashlib.sha256(cls._normalize_content(content).encode()).hexdigest()[:16]

    def _migrate_content_hash(self) -> None:
        """Idempotent migration: add + backfill the content_hash column.

        The LLM extractor writes near-identical facts every turn (overlapping
        10-message windows, fresh uuid each time), growing the table without
        bound. Dedupe keys on a normalized-content hash — exact-match dedupe
        (WHERE content = ?) missed case/whitespace variants.
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(memories)")}
        if "content_hash" in cols:
            return
        self._conn.execute("ALTER TABLE memories ADD COLUMN content_hash TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash)"
        )
        rows = self._conn.execute("SELECT id, content FROM memories").fetchall()
        for mem_id, content in rows:
            self._conn.execute(
                "UPDATE memories SET content_hash = ? WHERE id = ?",
                (self._content_hash(content), mem_id),
            )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection.

        Synchronous by design (called from test teardown / sync cleanup);
        a one-shot close at shutdown is acceptable — unlike per-turn I/O.
        """
        self._conn.close()

    async def prefetch(self, query: str) -> None:
        # Fire-and-forget: FTS5 is fast enough to query inline
        pass

    async def recall(self, query: str, k: int = 5) -> tuple[Memory, ...]:
        # Empty/blank query: FTS5 MATCH '' (and LIKE '%%') matches EVERY
        # row, which would leak all memories into context. Return nothing.
        if not query.strip():
            return ()
        # FTS5 MATCH uses a query-language grammar; raw user input containing
        # special chars (", AND, OR, NOT, *, (, NEAR) raises OperationalError.
        # Treat FTS5 syntax errors as "no match" and fall back to a LIKE scan
        # so a hostile/invalid query degrades gracefully instead of crashing.
        # Note: OperationalError is also raised for DB-level failures (locked,
        # malformed, closed connection). The LIKE fallback reuses the same
        # connection, so a permanent error propagates from the fallback too;
        # but we log here so a transient error that happens to succeed on the
        # fallback isn't silently reported as "no match".
        # CJK queries bypass FTS: unicode61 indexes CJK runs as single
        # tokens (no segmentation), so FTS MATCH misses substrings like
        # '代码' inside '用户的代码审查...'. LIKE substring is correct here.
        async with self._lock:
            return await asyncio.to_thread(self._recall_sync, query, k)

    def _recall_sync(self, query: str, k: int) -> tuple[Memory, ...]:
        from ..session.search import _CJK_RE

        if _CJK_RE.search(query):
            return self._like_search(query, k)
        try:
            return self._fts_search(query, k)
        except sqlite3.OperationalError as e:
            _logger.debug("FTS5 search fell back to LIKE for %r: %s", query, e)
            return self._like_search(query, k)

    def _fts_search(self, query: str, k: int) -> tuple[Memory, ...]:
        # Build a CJK-aware query: raw CJK input never matches because
        # unicode61 indexes CJK runs as single tokens (see search.py).
        from ..session.search import _build_fts_query

        fts_query = _build_fts_query(query)
        rows = self._conn.execute(
            "SELECT m.id, m.content, m.category, m.created_at, "
            "       -bm25(memories_fts) AS relevance_score, "
            "       m.session_id, m.visibility, m.metadata "
            "FROM memories_fts "
            "JOIN memories m ON memories_fts.rowid = m.rowid "
            "WHERE memories_fts MATCH ? "
            "ORDER BY relevance_score DESC LIMIT ?",
            (fts_query, k),
        ).fetchall()
        return self._rows_to_memories(rows)

    def _like_search(self, query: str, k: int) -> tuple[Memory, ...]:
        """LIKE-based fallback — no FTS5 grammar, safe for any input.

        Escapes LIKE wildcards (% and _) so a query like '50%' matches the
        literal substring rather than acting as a wildcard and matching
        nearly everything.
        """
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        rows = self._conn.execute(
            "SELECT m.id, m.content, m.category, m.created_at, "
            "       0.0 AS relevance_score, "
            "       m.session_id, m.visibility, m.metadata "
            "FROM memories m "
            "WHERE m.content LIKE ? ESCAPE '\\' "
            "LIMIT ?",
            (like, k),
        ).fetchall()
        return self._rows_to_memories(rows)

    @staticmethod
    def _rows_to_memories(rows) -> tuple[Memory, ...]:
        return tuple(
            Memory(
                id=r[0],
                content=r[1],
                category=r[2],
                created_at=r[3],
                relevance_score=r[4],
                session_id=r[5],
                visibility=r[6],
                metadata=json.loads(r[7]) if r[7] else None,
            )
            for r in rows
        )

    async def sync_turn(self, session_id: str, history: tuple[Message, ...]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._sync_turn_sync, session_id, history)

    def _sync_turn_sync(self, session_id: str, history: tuple[Message, ...]) -> None:
        # Store recent messages as basic context memories
        now = time.time()
        for i, msg in enumerate(history[-5:]):  # last 5 messages
            if not msg.content.strip():
                continue
            mem_id = f"{session_id}-{now}-{i}"
            self._insert(
                Memory(
                    id=mem_id,
                    content=msg.content[:500],  # truncate long messages
                    category="context",
                    created_at=now,
                    session_id=session_id,
                )
            )

    async def batch_write(self, memories: tuple[Memory, ...]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._batch_write_sync, memories)

    def _batch_write_sync(self, memories: tuple[Memory, ...]) -> None:
        for m in memories:
            if self.write_approval:
                self._insert_pending(m)
            else:
                self._insert(m)

    def _evict_overflow(self) -> None:
        """Rolling cap: delete oldest memories beyond MAX_MEMORIES.

        Eviction order: oldest first, category='context' entries before
        others at the same age (they are raw conversation-window echoes —
        the least durable; facts/preferences are the distilled ones).
        """
        count = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        overflow = count - self.MAX_MEMORIES
        if overflow <= 0:
            return
        rows = self._conn.execute(
            "SELECT rowid, id, content, category FROM memories "
            "ORDER BY created_at ASC, CASE category WHEN 'context' THEN 0 ELSE 1 END ASC"
        ).fetchall()
        for rowid, mem_id, content, _category in rows[:overflow]:
            self._conn.execute(
                "INSERT INTO memories_fts(memories_fts, rowid, content) "
                "VALUES('delete', ?, ?)",
                (rowid, content),
            )
            self._conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        if overflow:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Write-approval gate (Hermes /memory semantics)
    # ------------------------------------------------------------------

    async def pending_memories(self) -> tuple[Memory, ...]:
        """List memories held for approval (write_approval=True mode)."""
        async with self._lock:
            return await asyncio.to_thread(self._pending_sync)

    def _pending_sync(self) -> tuple[Memory, ...]:
        rows = self._conn.execute(
            "SELECT id, content, category, created_at, session_id, "
            "visibility, metadata FROM pending_memories ORDER BY created_at"
        ).fetchall()
        return tuple(
            Memory(
                id=r[0], content=r[1], category=r[2], created_at=r[3],
                session_id=r[4], visibility=r[5],
                metadata=json.loads(r[6]) if r[6] else None,
            )
            for r in rows
        )

    async def approve_memory(self, memory_id: str) -> None:
        """Approve one pending memory — move it into live memories."""
        async with self._lock:
            await asyncio.to_thread(self._approve_sync, memory_id)

    def _approve_sync(self, memory_id: str) -> None:
        row = self._conn.execute(
            "SELECT id, content, category, created_at, session_id, "
            "visibility, metadata, content_hash FROM pending_memories "
            "WHERE id = ?", (memory_id,),
        ).fetchone()
        if not row:
            return
        self._conn.execute("DELETE FROM pending_memories WHERE id = ?", (memory_id,))
        self._conn.commit()
        self._insert(Memory(
            id=row[0], content=row[1], category=row[2], created_at=row[3],
            session_id=row[4], visibility=row[5],
            metadata=json.loads(row[6]) if row[6] else None,
        ))

    async def reject_memory(self, memory_id: str) -> None:
        """Reject one pending memory — discard it."""
        async with self._lock:
            await asyncio.to_thread(self._reject_sync, memory_id)

    def _reject_sync(self, memory_id: str) -> None:
        self._conn.execute("DELETE FROM pending_memories WHERE id = ?", (memory_id,))
        self._conn.commit()

    def _insert_pending(self, m: Memory) -> None:
        """Insert into the pending table (write_approval mode).

        The pending table is plain SQLite (no FTS) — entries only enter
        the FTS index when approved via _insert.
        """
        content_hash = self._content_hash(m.content)
        if self._conn.execute(
            "SELECT id FROM pending_memories WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        ).fetchone():
            return  # same dedupe contract as _insert
        self._conn.execute(
            "INSERT INTO pending_memories "
            "(id, content, category, created_at, session_id, visibility, metadata, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.id, m.content, m.category, m.created_at,
                m.session_id, m.visibility,
                json.dumps(m.metadata) if m.metadata else None,
                content_hash,
            ),
        )
        self._conn.commit()

    async def delete(self, memory_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, memory_id)

    def _delete_sync(self, memory_id: str) -> None:
        row = self._conn.execute(
            "SELECT rowid, content FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row:
            # External-content FTS5 requires the delete command to carry the
            # exact text that was originally indexed; passing '' makes the
            # delete a silent no-op and corrupts the index over time.
            self._conn.execute(
                "INSERT INTO memories_fts(memories_fts, rowid, content) "
                "VALUES('delete', ?, ?)",
                (row[0], row[1]),
            )
        self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._conn.commit()

    def system_prompt_block(self) -> str:
        return ""

    def _insert(self, m: Memory) -> None:
        # Normalized-content dedupe: the extractor re-derives the same fact
        # from overlapping windows with a fresh uuid each turn. Skip when
        # ANY existing row has the same canonical content.
        content_hash = self._content_hash(m.content)
        if self._conn.execute(
            "SELECT id FROM memories WHERE content_hash = ? LIMIT 1", (content_hash,)
        ).fetchone():
            return
        # INSERT OR REPLACE changed the rowid on every rewrite, orphaning
        # the old rowid's FTS entry (external-content FTS5 doesn't sync
        # itself) — index bloat plus stale tokens re-attached to unrelated
        # memories when the rowid was reused. Explicit handling instead:
        existing = self._conn.execute(
            "SELECT rowid, content FROM memories WHERE id = ?", (m.id,)
        ).fetchone()
        if existing and existing[1] == m.content:
            return  # idempotent re-write — nothing to do
        if existing:
            # Clear the old FTS entry (delete requires the exact original
            # text, same contract as delete()) then the old row.
            self._conn.execute(
                "INSERT INTO memories_fts(memories_fts, rowid, content) "
                "VALUES('delete', ?, ?)",
                (existing[0], existing[1]),
            )
            self._conn.execute("DELETE FROM memories WHERE id = ?", (m.id,))
        self._conn.execute(
            "INSERT INTO memories "
            "(id, content, category, created_at, session_id, visibility, metadata, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.id,
                m.content,
                m.category,
                m.created_at,
                m.session_id,
                m.visibility,
                json.dumps(m.metadata) if m.metadata else None,
                content_hash,
            ),
        )
        # Sync to FTS5 content table
        rowid = self._conn.execute("SELECT rowid FROM memories WHERE id = ?", (m.id,)).fetchone()
        if rowid:
            self._conn.execute(
                "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                (rowid[0], m.content),
            )
        # Commit so writes survive a process restart — Python's sqlite3
        # defaults to a manual transaction; without commit(), close() rolls
        # back every insert and memory silently disappears across restarts.
        self._conn.commit()
        self._evict_overflow()

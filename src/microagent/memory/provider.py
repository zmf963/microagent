"""Memory data model, MemoryProvider Protocol, and SQLiteMemoryProvider.

Cross-session memory is optional — no memory code runs unless a
MemoryProvider is configured. The default SQLite+FTS5 implementation
provides full-text search with zero external dependencies.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.types import Message

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
        metadata TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content, content='memories', content_rowid='rowid'
    );
    """

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.executescript(self.SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    async def prefetch(self, query: str) -> None:
        # Fire-and-forget: FTS5 is fast enough to query inline
        pass

    async def recall(self, query: str, k: int = 5) -> tuple[Memory, ...]:
        # FTS5 MATCH uses a query-language grammar; raw user input containing
        # special chars (", AND, OR, NOT, *, (, NEAR) raises OperationalError.
        # Treat FTS5 syntax errors as "no match" and fall back to a LIKE scan
        # so a hostile/invalid query degrades gracefully instead of crashing.
        try:
            return self._fts_search(query, k)
        except sqlite3.OperationalError:
            return self._like_search(query, k)

    def _fts_search(self, query: str, k: int) -> tuple[Memory, ...]:
        rows = self._conn.execute(
            "SELECT m.id, m.content, m.category, m.created_at, "
            "       -bm25(memories_fts) AS relevance_score, "
            "       m.session_id, m.visibility, m.metadata "
            "FROM memories_fts "
            "JOIN memories m ON memories_fts.rowid = m.rowid "
            "WHERE memories_fts MATCH ? "
            "ORDER BY relevance_score DESC LIMIT ?",
            (query, k),
        ).fetchall()
        return self._rows_to_memories(rows)

    def _like_search(self, query: str, k: int) -> tuple[Memory, ...]:
        """LIKE-based fallback — no FTS5 grammar, safe for any input."""
        like = f"%{query}%"
        rows = self._conn.execute(
            "SELECT m.id, m.content, m.category, m.created_at, "
            "       0.0 AS relevance_score, "
            "       m.session_id, m.visibility, m.metadata "
            "FROM memories m "
            "WHERE m.content LIKE ? "
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
        for m in memories:
            self._insert(m)

    async def delete(self, memory_id: str) -> None:
        row = self._conn.execute(
            "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row:
            # External content FTS5 requires explicit 'delete' command
            self._conn.execute(
                "INSERT INTO memories_fts(memories_fts, rowid, content) "
                "VALUES('delete', ?, '')",
                (row[0],),
            )
        self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._conn.commit()

    def system_prompt_block(self) -> str:
        return ""

    def _insert(self, m: Memory) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO memories "
            "(id, content, category, created_at, session_id, visibility, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                m.id,
                m.content,
                m.category,
                m.created_at,
                m.session_id,
                m.visibility,
                json.dumps(m.metadata) if m.metadata else None,
            ),
        )
        # Sync to FTS5 content table
        rowid = self._conn.execute("SELECT rowid FROM memories WHERE id = ?", (m.id,)).fetchone()
        if rowid:
            self._conn.execute(
                "INSERT OR REPLACE INTO memories_fts(rowid, content) VALUES (?, ?)",
                (rowid[0], m.content),
            )
        # Commit so writes survive a process restart — Python's sqlite3
        # defaults to a manual transaction; without commit(), close() rolls
        # back every insert and memory silently disappears across restarts.
        self._conn.commit()

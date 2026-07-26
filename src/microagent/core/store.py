"""SQLite WAL store — durable session persistence.

Stores messages per session_id. Supports:
- ``append``: add a message to a session
- ``load_history``: retrieve all messages for a session
- ``checkpoint``: force WAL checkpoint (truncate)

Design (from design doc §2.3 + Appendix C.2):
- WAL mode for crash recovery.
- Messages are JSON-serialised for storage.
- In-memory list is the primary read path; SQLite is for durability.
- All SQLite I/O runs in a thread via asyncio.to_thread() to avoid
  blocking the event loop.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .types import Message, ToolCall, Usage

# ---------------------------------------------------------------------------
# Store Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Store(Protocol):
    """Persistent session store — append messages, load history, list sessions."""

    async def append(self, session_id: str, msg: Message) -> None: ...
    async def load_history(self, session_id: str) -> list[Message]: ...
    async def checkpoint(self, session_id: str) -> None: ...
    async def list_sessions(self) -> list[str]: ...
    async def session_summaries(self) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Message serialization
# ---------------------------------------------------------------------------


def _serialize_message(msg: Message) -> str:
    """Serialize a Message to JSON for SQLite storage."""
    d: dict[str, Any] = {"role": msg.role, "content": msg.content}
    if msg.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls
        ]
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    if msg.usage:
        d["usage"] = {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
            "cost_usd": msg.usage.cost_usd,
        }
    if msg.is_error:
        d["is_error"] = True
    return json.dumps(d, ensure_ascii=False)


def _deserialize_message(data: str) -> Message:
    """Deserialize a Message from JSON."""
    d = json.loads(data)
    tool_calls = ()
    if "tool_calls" in d:
        tool_calls = tuple(
            ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
            for tc in d["tool_calls"]
        )
    usage = None
    if "usage" in d:
        usage = Usage(
            input_tokens=d["usage"]["input_tokens"],
            output_tokens=d["usage"]["output_tokens"],
            cost_usd=d["usage"].get("cost_usd", 0.0),
        )
    return Message(
        role=d["role"],
        content=d["content"],
        tool_calls=tool_calls,
        tool_call_id=d.get("tool_call_id"),
        usage=usage,
        is_error=d.get("is_error", False),
    )


# ---------------------------------------------------------------------------
# SQLiteStore
# ---------------------------------------------------------------------------


class SQLiteStore:
    """SQLite WAL-mode store for session persistence.

    All I/O runs via asyncio.to_thread() to avoid blocking the event loop.
    Uses check_same_thread=False because the connection is accessed from
    worker threads, but all access is serialized by an asyncio.Lock.

    Design note: the lock serializes ALL operations (reads + writes),
    which means concurrent reads block each other. SQLite WAL mode
    supports concurrent reads with a single writer, so a read-write lock
    could improve throughput. However, for single-agent workloads the
    contention window is negligible, so a simple mutex is kept for
    correctness and simplicity.
    """

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,  # autocommit
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._lock = asyncio.Lock()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                data TEXT NOT NULL,
                UNIQUE(session_id, seq)
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON messages(session_id, seq)")

    async def append(self, session_id: str, msg: Message) -> None:
        serialized = _serialize_message(msg)

        def _append():
            row = self._conn.execute(
                "SELECT MAX(seq) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = (row[0] or 0) + 1
            self._conn.execute(
                "INSERT INTO messages (session_id, seq, data) VALUES (?, ?, ?)",
                (session_id, seq, serialized),
            )

        async with self._lock:
            await asyncio.to_thread(_append)

    async def load_history(self, session_id: str) -> list[Message]:
        def _load():
            rows = self._conn.execute(
                "SELECT data FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
            return [_deserialize_message(r[0]) for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_load)

    async def checkpoint(self, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                lambda: self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            )

    async def list_sessions(self) -> list[str]:
        def _list():
            rows = self._conn.execute(
                "SELECT session_id FROM messages GROUP BY session_id ORDER BY MAX(id) DESC"
            ).fetchall()
            return [r[0] for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_list)

    async def session_summaries(self) -> list[dict[str, Any]]:
        """Return count + last-message-preview per session in one query.

        Avoids the O(N) per-session load_history calls in /list.
        Returns list of dicts: {session_id, count, preview}.
        """
        def _summaries():
            rows = self._conn.execute("""
                SELECT
                    session_id,
                    COUNT(*) AS count,
                    (SELECT data FROM messages m2
                     WHERE m2.session_id = m.session_id
                     ORDER BY m2.seq DESC LIMIT 1) AS last_data
                FROM messages m
                GROUP BY session_id
                ORDER BY MAX(m.id) DESC
            """).fetchall()
            summaries = []
            for r in rows:
                session_id, count, last_data = r
                preview = ""
                if last_data:
                    try:
                        msg = _deserialize_message(last_data)
                        preview = msg.content[:50].replace("\n", " ")
                    except Exception:
                        pass
                summaries.append({"session_id": session_id, "count": count, "preview": preview})
            return summaries

        async with self._lock:
            return await asyncio.to_thread(_summaries)

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# InMemoryStore — for testing without disk I/O
# ---------------------------------------------------------------------------


class InMemoryStore:
    """Simple dict-based store for unit tests.

    Maintains a global append counter to match SQLiteStore's
    ``ORDER BY MAX(id) DESC`` recency ordering.
    """

    def __init__(self):
        self._data: dict[str, list[Message]] = {}
        self._seq: int = 0  # global append counter
        self._last_seq: dict[str, int] = {}  # session_id → last append seq

    async def append(self, session_id: str, msg: Message) -> None:
        self._data.setdefault(session_id, []).append(msg)
        self._seq += 1
        self._last_seq[session_id] = self._seq

    async def load_history(self, session_id: str) -> list[Message]:
        return list(self._data.get(session_id, []))

    async def checkpoint(self, session_id: str) -> None:
        pass

    async def list_sessions(self) -> list[str]:
        # Return sessions in recency order (most recently appended first)
        return sorted(self._last_seq, key=lambda s: self._last_seq[s], reverse=True)

    async def session_summaries(self) -> list[dict[str, Any]]:
        sorted_sids = sorted(self._last_seq, key=lambda s: self._last_seq[s], reverse=True)
        summaries = []
        for sid in sorted_sids:
            msgs = self._data.get(sid, [])
            preview = ""
            if msgs:
                preview = msgs[-1].content[:50].replace("\n", " ")
            summaries.append({"session_id": sid, "count": len(msgs), "preview": preview})
        return summaries

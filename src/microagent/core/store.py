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
import logging
import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .types import Message, ToolCall, Usage

logger = logging.getLogger(__name__)


def _apply_folds(
    rows: list[tuple[int, Message]],
    folds: list[tuple],
    session_id: str,
) -> list[tuple[int, Message]]:
    """Apply replace folds to (seq, message) rows, in order.

    Each fold (kind, start, end, repl_start, repl_end): remove the
    shadowed seq range [start..end], then move the replacement block
    [repl_start..repl_end] (which sits at the log tail where it was
    appended) into the vacated position. Validation (dsh SurfaceManager
    double-check parity): all referenced seqs must exist in the current
    surface and the block must lie strictly after the shadowed range;
    an invalid fold is skipped with a warning instead of corrupting the
    derived surface.
    """
    for kind, start, end, rstart, rend in folds:
        index = {s: i for i, (s, _) in enumerate(rows)}
        if (
            start not in index
            or end not in index
            or rstart not in index
            or rend not in index
        ):
            logger.warning(
                "surface fold %r in session %s references missing seqs "
                "(%d..%d → %d..%d) — skipping",
                kind, session_id, start, end, rstart, rend,
            )
            continue
        if not (start <= end < rstart <= rend):
            logger.warning(
                "surface fold %r in session %s has invalid ranges "
                "(%d..%d → %d..%d) — skipping",
                kind, session_id, start, end, rstart, rend,
            )
            continue
        # Insertion point = where the shadowed range STARTED, captured
        # before removal (not "first seq > end": after earlier folds the
        # surviving messages around the gap can have seqs on both sides).
        insert_at = index[start]
        block = [r for r in rows if rstart <= r[0] <= rend]
        remaining = [
            r for r in rows if not (start <= r[0] <= end or rstart <= r[0] <= rend)
        ]
        insert_at = min(insert_at, len(remaining))
        remaining[insert_at:insert_at] = block
        rows = remaining
    return rows


class UnsupportedSessionError(Exception):
    """A session row uses an event kind this library version cannot read.

    Raised by _deserialize_message when a stored row has an unknown
    ``kind`` not marked ignorable. A future-versioned session must not
    load as a subtly-wrong history — fail loudly instead.
    """

# ---------------------------------------------------------------------------
# Store Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Store(Protocol):
    """Persistent session store — append messages, load history, list sessions.

    ``append`` returns the assigned per-session seq when the store
    supports it (SQLiteStore/InMemoryStore do); custom stores may return
    None — surface fold recording (compaction event sourcing) is simply
    skipped for those.
    """

    async def append(self, session_id: str, msg: Message) -> int | None: ...
    async def load_history(self, session_id: str) -> list[Message]: ...
    async def checkpoint(self, session_id: str) -> None: ...
    async def list_sessions(self) -> list[str]: ...
    async def session_summaries(self) -> list[dict[str, Any]]: ...
    async def record_llm_retry(self, session_id: str, code: str, delay_ms: int) -> None: ...
    async def last_llm_retry(self, session_id: str, code: str | None = None) -> tuple[str, int] | None: ...
    async def flush(self, session_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Message serialization
# ---------------------------------------------------------------------------


def _serialize_message(msg: Message) -> str:
    """Serialize a Message to JSON for SQLite storage."""
    d: dict[str, Any] = {"role": msg.role, "content": msg.content, "kind": "message"}
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
    """Deserialize a Message from JSON.

    deepseek-harness parity (ignorable-defaulted-required vocabulary):
    an unknown ``kind`` that is NOT marked ignorable raises
    UnsupportedSessionError instead of being silently misread — a
    future-versioned session must never load as a subtly-wrong history.
    """
    d = json.loads(data)
    kind = d.get("kind", "message")
    if kind != "message" and not d.get("ignorable", False):
        raise UnsupportedSessionError(
            f"unsupported session event kind {kind!r} (not ignorable)"
        )
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
        # LLM retry ledger (deepseek-harness parity: retry history
        # reconstructed from the session log, not memory). Backoff
        # continuation survives process restarts — the runner reads the
        # last matching row instead of in-memory counters.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_retry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts REAL NOT NULL,
                code TEXT NOT NULL,
                delay_ms INTEGER NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_retry ON llm_retry(session_id, id)"
        )
        # Surface fold ops (dsh surfaceOp replace parity): compaction is
        # recorded as a replace {start_seq..end_seq} → replacement block
        # {repl_start..repl_end} instead of mutating history in memory
        # only. The RAW log stays complete; the LLM-visible surface is
        # derived by applying folds in order (load_surface). Resume no
        # longer reloads the uncompacted history and re-compresses
        # (extra LLM call + broken incremental-summary chain).
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS surface_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                start_seq INTEGER NOT NULL,
                end_seq INTEGER NOT NULL,
                repl_start INTEGER NOT NULL,
                repl_end INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_surface_ops ON surface_ops(session_id, id)"
        )

    async def append(self, session_id: str, msg: Message) -> int | None:
        """Append one message; returns the assigned per-session seq."""
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
            return seq

        async with self._lock:
            return await asyncio.to_thread(_append)

    async def load_history(self, session_id: str) -> list[Message]:
        def _load():
            rows = self._conn.execute(
                "SELECT data FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
            # Per-row tolerance, mirroring session_summaries: one corrupt
            # JSON blob (disk corruption, interrupted write) must not kill
            # the whole session resume path (CLI / cron / runner).
            # UnsupportedSessionError is NOT swallowed — an unknown
            # non-ignorable kind must fail loudly, not load as a
            # subtly-wrong history (dsh ignorable-defaulted-required).
            out: list[Message] = []
            for r in rows:
                try:
                    out.append(_deserialize_message(r[0]))
                except UnsupportedSessionError:
                    raise
                except Exception:
                    continue
            return out

        async with self._lock:
            return await asyncio.to_thread(_load)

    # ------------------------------------------------------------------
    # Surface fold ops (dsh surfaceOp replace parity)
    # ------------------------------------------------------------------

    def _load_rows_sync(self, session_id: str) -> list[tuple[int, Message]]:
        rows = self._conn.execute(
            "SELECT seq, data FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        out: list[tuple[int, Message]] = []
        for seq, blob in rows:
            try:
                out.append((seq, _deserialize_message(blob)))
            except UnsupportedSessionError:
                raise
            except Exception:
                continue
        return out

    def _folds_sync(self, session_id: str) -> list[tuple]:
        rows = self._conn.execute(
            "SELECT kind, start_seq, end_seq, repl_start, repl_end "
            "FROM surface_ops WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [tuple(r) for r in rows]

    async def record_fold(
        self,
        session_id: str,
        kind: str,
        start_seq: int,
        end_seq: int,
        repl_start: int,
        repl_end: int,
    ) -> None:
        """Record one compaction fold: the surface range
        [start_seq, end_seq] is replaced by the already-appended block
        [repl_start, repl_end]. kind: 'summary' (L3) | 'fallback'."""
        import time as _time

        def _rec():
            self._conn.execute(
                "INSERT INTO surface_ops "
                "(session_id, kind, start_seq, end_seq, repl_start, repl_end, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, kind, start_seq, end_seq, repl_start, repl_end, _time.time()),
            )
            self._conn.commit()

        async with self._lock:
            await asyncio.to_thread(_rec)

    async def load_surface_with_seqs(
        self, session_id: str
    ) -> tuple[list[Message], list[int]]:
        """Derive the LLM-visible surface: raw log + folds applied in order.

        Returns (messages, seqs) aligned positionally — the seq sidecar
        lets the runner keep in-memory state mappable to the log across
        multiple nested folds. Each fold is double-checked on apply
        (dsh parity): the shadowed range and the replacement block must
        both exist; a fold failing validation is skipped with a warning
        rather than corrupting the surface.
        """

        def _derive():
            rows = self._load_rows_sync(session_id)
            folds = self._folds_sync(session_id)
            applied = _apply_folds(rows, folds, session_id)
            return ([m for _, m in applied], [s for s, _ in applied])

        async with self._lock:
            return await asyncio.to_thread(_derive)

    async def load_surface(self, session_id: str) -> list[Message]:
        msgs, _ = await self.load_surface_with_seqs(session_id)
        return msgs

    async def last_fold_summary(self, session_id: str) -> Message | None:
        """The summary message of the most recent 'summary' fold — used to
        rehydrate CompactionState.previous_summary across restarts, keeping
        the incremental-summary chain alive across processes."""

        def _last():
            row = self._conn.execute(
                "SELECT repl_end FROM surface_ops "
                "WHERE session_id = ? AND kind = 'summary' "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            msg_row = self._conn.execute(
                "SELECT data FROM messages WHERE session_id = ? AND seq = ?",
                (session_id, row[0]),
            ).fetchone()
            if msg_row is None:
                return None
            try:
                return _deserialize_message(msg_row[0])
            except Exception:
                return None

        async with self._lock:
            return await asyncio.to_thread(_last)

    async def checkpoint(self, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                lambda: self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            )

    async def flush(self, session_id: str) -> None:
        """Durability barrier: make this session's writes visible to other
        connections/processes before reporting turn completion.

        dsh session/flush parity. PASSIVE keeps the WAL in place (fast,
        non-blocking) while ensuring readers on other connections see
        the committed rows.
        """
        async with self._lock:
            await asyncio.to_thread(
                lambda: self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            )

    async def record_llm_retry(
        self, session_id: str, code: str, delay_ms: int
    ) -> None:
        """Append one LLM retry event to the ledger.

        The ledger is pruned to the most recent 100 rows per session —
        a flaky gateway writing dozens of rows per session must not grow
        sessions.db without bound.
        """
        import time

        def _record():
            self._conn.execute(
                "INSERT INTO llm_retry (session_id, ts, code, delay_ms) "
                "VALUES (?, ?, ?, ?)",
                (session_id, time.time(), code, delay_ms),
            )
            self._conn.execute(
                "DELETE FROM llm_retry WHERE session_id = ? AND id NOT IN ("
                "SELECT id FROM llm_retry WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 100)",
                (session_id, session_id),
            )

        async with self._lock:
            await asyncio.to_thread(_record)

    async def last_llm_retry(
        self, session_id: str, code: str | None = None
    ) -> tuple[str, int] | None:
        """Return (code, delay_ms) of the last retry for this session.

        ``code`` filters to matching failure codes when given. None when
        the session has no recorded retry — backoff starts fresh.
        """
        def _last():
            if code is None:
                row = self._conn.execute(
                    "SELECT code, delay_ms FROM llm_retry "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT code, delay_ms FROM llm_retry "
                    "WHERE session_id = ? AND code = ? ORDER BY id DESC LIMIT 1",
                    (session_id, code),
                ).fetchone()
            return row

        async with self._lock:
            return await asyncio.to_thread(_last)

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
    ``ORDER BY MAX(id) DESC`` recency ordering. Rows are (seq, Message)
    pairs and folds mirror SQLiteStore's surface_ops so tests exercise
    the same derive logic.
    """

    def __init__(self):
        self._rows: dict[str, list[tuple[int, Message]]] = {}
        self._folds: dict[str, list[tuple]] = {}  # (kind, s, e, rs, re)
        self._seq: int = 0  # global append counter
        self._last_seq: dict[str, int] = {}  # session_id → last append seq
        self._retries: list[tuple[str, str, int]] = []  # (session_id, code, delay_ms)

    async def record_llm_retry(
        self, session_id: str, code: str, delay_ms: int
    ) -> None:
        self._retries.append((session_id, code, delay_ms))

    async def last_llm_retry(
        self, session_id: str, code: str | None = None
    ) -> tuple[str, int] | None:
        for sid, c, delay in reversed(self._retries):
            if sid == session_id and (code is None or c == code):
                return (c, delay)
        return None

    async def append(self, session_id: str, msg: Message) -> int | None:
        self._rows.setdefault(session_id, []).append((self._seq + 1, msg))
        self._seq += 1
        self._last_seq[session_id] = self._seq
        return self._seq

    async def record_fold(
        self,
        session_id: str,
        kind: str,
        start_seq: int,
        end_seq: int,
        repl_start: int,
        repl_end: int,
    ) -> None:
        self._folds.setdefault(session_id, []).append(
            (kind, start_seq, end_seq, repl_start, repl_end)
        )

    async def load_surface_with_seqs(
        self, session_id: str
    ) -> tuple[list[Message], list[int]]:
        rows = list(self._rows.get(session_id, []))
        folds = self._folds.get(session_id, [])
        applied = _apply_folds(rows, folds, session_id)
        return ([m for _, m in applied], [s for s, _ in applied])

    async def load_surface(self, session_id: str) -> list[Message]:
        msgs, _ = await self.load_surface_with_seqs(session_id)
        return msgs

    async def last_fold_summary(self, session_id: str) -> Message | None:
        for kind, _s, _e, _rs, rend in reversed(self._folds.get(session_id, [])):
            if kind != "summary":
                continue
            for s, m in reversed(self._rows.get(session_id, [])):
                if s == rend:
                    return m
        return None

    async def load_history(self, session_id: str) -> list[Message]:
        return [m for _, m in self._rows.get(session_id, [])]

    async def checkpoint(self, session_id: str) -> None:
        pass

    async def flush(self, session_id: str) -> None:
        pass

    async def list_sessions(self) -> list[str]:
        # Return sessions in recency order (most recently appended first)
        return sorted(self._last_seq, key=lambda s: self._last_seq[s], reverse=True)

    async def session_summaries(self) -> list[dict[str, Any]]:
        sorted_sids = sorted(self._last_seq, key=lambda s: self._last_seq[s], reverse=True)
        summaries = []
        for sid in sorted_sids:
            rows = self._rows.get(sid, [])
            preview = ""
            if rows:
                preview = rows[-1][1].content[:50].replace("\n", " ")
            summaries.append({"session_id": sid, "count": len(rows), "preview": preview})
        return summaries

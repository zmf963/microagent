"""session_search — full-text search across all past sessions.

Searches the SQLiteStore's messages table (JSON-serialized) with LIKE.
"""

from __future__ import annotations

import json

from ..core.types import Message
from ..core.store import Store, SQLiteStore


async def search_sessions(
    store: Store,
    query: str,
    k: int = 5,
) -> tuple[Message, ...]:
    """Search all stored sessions for messages matching query.

    Uses SQLite LIKE on the JSON data column. Returns up to k messages.
    """
    if not isinstance(store, SQLiteStore):
        return ()

    safe_query = query.replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{safe_query}%"

    rows = store._conn.execute(
        "SELECT data FROM messages "
        "WHERE data LIKE ? ESCAPE '\\' "
        "ORDER BY id DESC LIMIT ?",
        (pattern, k),
    ).fetchall()

    results = []
    for (data,) in rows:
        try:
            obj = json.loads(data)
            results.append(Message(
                role=obj.get("role", "user"),
                content=obj.get("content", ""),
            ))
        except (json.JSONDecodeError, KeyError):
            continue

    return tuple(results)

"""Regression test: SQLiteMemoryProvider must persist writes across restarts.

Before the fix, _insert() never called conn.commit(). Python's sqlite3
defaults to a manual transaction, so close() rolled back every insert.
Memory worked mid-session (visible on the same uncommitted connection)
but vanished on the next process start — defeating the entire purpose of
a cross-session memory store.
"""

import pytest

from microagent.memory.provider import SQLiteMemoryProvider, Memory
import time


@pytest.mark.asyncio
async def test_writes_persist_across_restart(tmp_path):
    """Write → close → reopen → recall must find the memory."""
    db = tmp_path / "mem.db"

    # First session: write a memory and close.
    prov = SQLiteMemoryProvider(db)
    await prov.batch_write((
        Memory(
            id="m1",
            content="The user prefers dark mode and concise answers.",
            category="preference",
            created_at=time.time(),
        ),
    ))
    prov.close()

    # Second session: reopen and recall.
    prov2 = SQLiteMemoryProvider(db)
    hits = await prov2.recall("dark mode", k=5)
    assert len(hits) == 1, f"expected 1 hit, got {len(hits)} (memory was lost on restart)"
    assert hits[0].id == "m1"
    assert "dark mode" in hits[0].content
    prov2.close()


@pytest.mark.asyncio
async def test_delete_persists_across_restart(tmp_path):
    """Delete must survive a restart (was rolled back before the fix)."""
    db = tmp_path / "mem2.db"

    prov = SQLiteMemoryProvider(db)
    await prov.batch_write((
        Memory(id="keep", content="keep this one", category="fact", created_at=time.time()),
        Memory(id="drop", content="drop this one", category="fact", created_at=time.time()),
    ))
    prov.close()

    # Reopen, delete, close.
    prov2 = SQLiteMemoryProvider(db)
    await prov2.delete("drop")
    prov2.close()

    # Reopen again — 'drop' must still be gone.
    prov3 = SQLiteMemoryProvider(db)
    hits = await prov3.recall("drop", k=5)
    assert len(hits) == 0, "delete was rolled back on restart"
    keep_hits = await prov3.recall("keep", k=5)
    assert len(keep_hits) == 1
    prov3.close()


@pytest.mark.asyncio
async def test_sync_turn_persists(tmp_path):
    """sync_turn (used by the runner after each turn) must persist too."""
    db = tmp_path / "mem3.db"
    from microagent.core.types import Message

    prov = SQLiteMemoryProvider(db)
    await prov.sync_turn(
        "sess-1",
        (
            Message.user("What is the capital of France?"),
            Message.assistant("Paris."),
        ),
    )
    prov.close()

    prov2 = SQLiteMemoryProvider(db)
    hits = await prov2.recall("capital", k=5)
    assert len(hits) >= 1
    prov2.close()

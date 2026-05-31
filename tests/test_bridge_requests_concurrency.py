"""Concurrency + durability tests for the production dispatch request queue
(atelier#81 — `bridge_requests`, migrations/shared/008).

Distinct from `tests/test_bridge_concurrency.py` (which stresses the INTER-AGENT
message wire `bridge_messages`). This suite targets the orchestrator↔Python
harness-call queue under SQLite WAL:

1. **create_team blocking-poll vs servicer-write race.** The Python
   `QueueBridgeDispatchTools.create_team` poll loop and the orchestrator servicer
   write the SAME row from two threads. With WAL + busy_timeout neither
   deadlocks; the poll observes the committed 'ready' and returns the team_id.
2. **Fire-and-forget enqueue is durable.** A spawn_*/send_message row is
   persisted `status='pending'` and survives even when the servicer NEVER runs —
   it is never silently dropped (fail-safe-pending).
3. **Concurrent enqueues do not lose or duplicate rows** under WAL writer
   serialization (the `BEGIN IMMEDIATE`-equivalent single-INSERT-per-commit
   path), and ids stay unique + monotonic.
4. **Idempotency: only 'pending' rows are claimable.** Two servicer passes flip
   a row once; the second pass is a no-op (a re-serviced row would double-spawn).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from scripts.dispatch import QueueBridgeDispatchTools
from scripts.migrate import apply_migrations

MIGRATIONS_SHARED = Path(__file__).resolve().parent.parent / "migrations" / "shared"


@pytest.fixture
def bridge_db(tmp_path):
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS_SHARED)
    return str(db)


def _service_pending_create_team(db_path, *, team_id, delay=0.0):
    """Servicer stand-in: wait `delay`, then flip the first pending create_team
    row to ready with the team_id. Runs in a thread to race the poller."""
    if delay:
        time.sleep(delay)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        row = con.execute(
            "SELECT id FROM bridge_requests WHERE kind='create_team' AND status='pending' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if row is not None:
            con.execute(
                "UPDATE bridge_requests SET status='ready', response_json=?, "
                "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (f'{{"team_id": "{team_id}"}}', row[0]),
            )
            con.commit()
    finally:
        con.close()


def test_create_team_poll_races_servicer_write_no_deadlock(bridge_db):
    """The blocking create_team poll (thread A) and the servicer write
    (thread B) hit the same row concurrently. WAL + busy_timeout means the poll
    sees the committed 'ready' and returns the team_id — no deadlock, no
    'database is locked'."""
    tools = QueueBridgeDispatchTools("cycle-1", db_path=bridge_db)

    servicer = threading.Thread(
        target=_service_pending_create_team,
        args=(bridge_db,),
        kwargs={"team_id": "T-RACE", "delay": 0.05},
        daemon=True,
    )
    servicer.start()
    # create_team blocks here, polling its own row while the servicer writes it.
    team_id = tools.create_team("cycle-team", ["pm-1", "sdet-1"])
    servicer.join(timeout=5)

    assert team_id == "T-RACE"
    # Exactly one create_team row, now ready.
    con = sqlite3.connect(bridge_db)
    try:
        rows = con.execute("SELECT status FROM bridge_requests WHERE kind='create_team'").fetchall()
    finally:
        con.close()
    assert [r[0] for r in rows] == ["ready"]


def test_fire_and_forget_enqueue_durable_without_servicer(bridge_db):
    """A fire-and-forget row persists status='pending' even when NO servicer
    ever runs — it is never silently dropped (durability / fail-safe-pending)."""
    tools = QueueBridgeDispatchTools("cycle-1", db_path=bridge_db)
    tools.spawn_subagent("task-1", 1, "PROMPT")
    tools.spawn_teammate("T", "sdet-1", "BRIEF")
    tools.send_message("T", "be-1", "MSG")

    # Re-open from a SEPARATE connection to prove the rows were committed (not
    # left in an uncommitted txn that vanishes on close).
    con = sqlite3.connect(bridge_db)
    try:
        rows = con.execute("SELECT kind, status FROM bridge_requests ORDER BY id").fetchall()
    finally:
        con.close()
    assert rows == [
        ("spawn_subagent", "pending"),
        ("spawn_teammate", "pending"),
        ("send_message", "pending"),
    ]


def test_concurrent_enqueues_no_loss_no_dup(bridge_db):
    """N threads each enqueue M fire-and-forget rows concurrently. Under WAL
    writer-serialization every row lands exactly once with a unique, monotonic
    id — no loss, no duplicate."""
    n_threads, per_thread = 8, 25
    tools = QueueBridgeDispatchTools("cycle-1", db_path=bridge_db)
    barrier = threading.Barrier(n_threads)

    def worker(tid):
        barrier.wait()  # maximize contention
        for i in range(per_thread):
            tools.spawn_subagent(f"t{tid}-{i}", 1, "P")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    con = sqlite3.connect(bridge_db)
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM bridge_requests ORDER BY id").fetchall()]
        total = con.execute("SELECT COUNT(*) FROM bridge_requests").fetchone()[0]
        distinct_args = con.execute(
            "SELECT COUNT(DISTINCT args_json) FROM bridge_requests"
        ).fetchone()[0]
    finally:
        con.close()

    expected = n_threads * per_thread
    assert total == expected, f"row loss/dup: expected {expected}, got {total}"
    assert len(set(ids)) == len(ids), "duplicate ids — id allocator raced"
    assert ids == sorted(ids), "ids not monotonic"
    # Each thread's payloads are unique (tid-i), so distinct args == total.
    assert distinct_args == expected


def test_idempotency_only_pending_rows_claimable(bridge_db):
    """A status flip is the 'claimed' key: a row already 'ready' is NOT picked
    up by a second servicer pass — re-servicing would double-spawn."""
    tools = QueueBridgeDispatchTools("cycle-1", db_path=bridge_db)
    tools.spawn_subagent("task-1", 1, "P")

    def claim_once():
        """Flip exactly the pending rows to ready; return how many it claimed."""
        con = sqlite3.connect(bridge_db)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            pending = con.execute(
                "SELECT id FROM bridge_requests WHERE status='pending'"
            ).fetchall()
            for (rid,) in pending:
                con.execute(
                    "UPDATE bridge_requests SET status='ready', response_json='{}' WHERE id=?",
                    (rid,),
                )
            con.commit()
            return len(pending)
        finally:
            con.close()

    assert claim_once() == 1  # first pass claims the one pending row
    assert claim_once() == 0  # second pass sees nothing pending — no double-claim

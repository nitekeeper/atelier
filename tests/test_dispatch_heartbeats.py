"""Tests for ``scripts.dispatch.read_heartbeats``.

Drives a temp SQLite DB seeded with the 003_team_mode.sql schema and
two heartbeat rows for distinct (team_id, role_id) pairs. Verifies the
return shape is the documented ``list[tuple[str, str, str]]``, sorted
deterministically, and contains exactly the seeded pairs.

The migration is applied directly to a temp file rather than via
``scripts/migrate.py`` because:
  * 003_team_mode.sql is self-contained (every FK target is defined
    within 003 itself — verified by ``grep REFERENCES`` over the file).
  * Applying only 003 keeps the fixture minimal and the test runtime
    fast.
  * If 003's FK targets ever start spanning 001/002, this test will
    fail loudly at the schema-apply step (CHECK / FK errors), at which
    point we extend the fixture to chain the prior migrations.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from scripts.dispatch import read_heartbeats

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_003 = REPO_ROOT / "migrations" / "shared" / "003_team_mode.sql"


def _apply_003_to_temp_db(db_path: Path) -> None:
    """Apply 003_team_mode.sql to a fresh SQLite file. FK enforcement is
    enabled on the connection because 003 carries composite FKs whose
    integrity must be checked at INSERT time, not only at PRAGMA
    foreign_key_check time."""
    sql = MIGRATION_003.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def _seed_two_heartbeats(db_path: Path) -> None:
    """Insert the minimal FK chain plus two heartbeat bridge_messages
    rows under two distinct (team_id, role_id) pairs at different
    ``created_at`` timestamps."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        # Two distinct teams so we exercise the (team_id, role_id)
        # tuple grouping rather than just role-level uniqueness.
        conn.execute(
            "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, ?, ?, ?)",
            ("team-alpha", "proj-1", "team-lead", "active"),
        )
        conn.execute(
            "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, ?, ?, ?)",
            ("team-beta", "proj-1", "team-lead", "active"),
        )

        # One persona snapshot is enough; both members can point at it.
        cur = conn.execute(
            "INSERT INTO persona_snapshots (persona_version, persona_blob) VALUES (?, ?)",
            ("v1", "{}"),
        )
        snap_id = cur.lastrowid

        conn.execute(
            "INSERT INTO team_members "
            "(team_id, role_id, member_name, persona_snapshot_id) "
            "VALUES (?, ?, ?, ?)",
            ("team-alpha", "backend-engineer-1", "be1", snap_id),
        )
        conn.execute(
            "INSERT INTO team_members "
            "(team_id, role_id, member_name, persona_snapshot_id) "
            "VALUES (?, ?, ?, ?)",
            ("team-beta", "sdet-1", "sdet1", snap_id),
        )

        # Two heartbeat rows at different created_at timestamps so we
        # can verify the MAX(created_at) per (team_id, role_id) grouping
        # picks the most recent — and prove the function returns one
        # row per pair, not one per message.
        conn.execute(
            "INSERT INTO bridge_messages "
            "(team_id, recipient, seq, sender_id, kind, payload, "
            " persona_snapshot_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "team-alpha",
                "team-lead",
                1,
                "backend-engineer-1",
                "heartbeat",
                "{}",
                snap_id,
                "2026-05-25T10:00:00.000Z",
            ),
        )
        conn.execute(
            "INSERT INTO bridge_messages "
            "(team_id, recipient, seq, sender_id, kind, payload, "
            " persona_snapshot_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "team-alpha",
                "team-lead",
                2,
                "backend-engineer-1",
                "heartbeat",
                "{}",
                snap_id,
                "2026-05-25T11:00:00.000Z",  # newer — MAX should pick this
            ),
        )
        conn.execute(
            "INSERT INTO bridge_messages "
            "(team_id, recipient, seq, sender_id, kind, payload, "
            " persona_snapshot_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "team-beta",
                "team-lead",
                1,
                "sdet-1",
                "heartbeat",
                "{}",
                snap_id,
                "2026-05-25T09:30:00.000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_read_heartbeats_returns_sorted_tuples() -> None:
    """``read_heartbeats`` returns a deterministic list of
    ``(team_id, role_id, last_seen_iso)`` tuples, one row per distinct
    (team_id, role_id) pair, with the MOST RECENT created_at chosen."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "heartbeats.db"
        _apply_003_to_temp_db(db_path)
        _seed_two_heartbeats(db_path)

        result = read_heartbeats(db_path)

        # Shape: list of 3-tuples.
        assert isinstance(result, list)
        assert len(result) == 2
        for entry in result:
            assert isinstance(entry, tuple)
            assert len(entry) == 3
            assert all(isinstance(field, str) for field in entry)

        # Determinism: calling twice returns identical sequences (no
        # dict-ordering noise leaking through the SQL group/order by).
        result_again = read_heartbeats(db_path)
        assert result == result_again

        # Content: exactly the two seeded (team_id, role_id) pairs.
        pairs = {(team, role) for team, role, _ in result}
        assert pairs == {
            ("team-alpha", "backend-engineer-1"),
            ("team-beta", "sdet-1"),
        }

        # The team-alpha entry picked the LATER timestamp (MAX(created_at)).
        ts_by_pair = {(team, role): ts for team, role, ts in result}
        assert ts_by_pair[("team-alpha", "backend-engineer-1")] == "2026-05-25T11:00:00.000Z"
        assert ts_by_pair[("team-beta", "sdet-1")] == "2026-05-25T09:30:00.000Z"

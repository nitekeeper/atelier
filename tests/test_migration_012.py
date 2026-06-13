"""Tests for migration 012: journal_attempts table.

Pattern mirrors existing atelier migration tests (e.g. test_bridge_payloads.py).

Coverage
--------
* Migration 012 file exists under migrations/shared/
* Schema applies cleanly to a fresh DB (shared migrations applied in order)
* journal_attempts table is created with the expected columns
* Migration is idempotent (applying shared/ twice does not raise or duplicate)
* Row INSERT / SELECT round-trip works (basic column contract)
* created_at has a DEFAULT that populates automatically
* Applying only shared/ (not local-only/) includes journal_attempts
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.migrate import apply_migrations

REPO_ROOT = Path(__file__).parent.parent
SHARED = REPO_ROOT / "migrations" / "shared"
MIGRATION_012 = SHARED / "012_result_journal.sql"


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh DB with all shared migrations applied."""
    db_path = str(tmp_path / "atelier.db")
    apply_migrations(db_path, SHARED)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── file existence ─────────────────────────────────────────────────────────


def test_migration_012_file_exists():
    assert MIGRATION_012.exists(), (
        f"migrations/shared/012_result_journal.sql is missing; "
        f"files present: {sorted(f.name for f in SHARED.glob('*.sql'))}"
    )


# ── schema shape ──────────────────────────────────────────────────────────


def test_journal_attempts_table_created(db: sqlite3.Connection):
    tables = {
        row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "journal_attempts" in tables, "journal_attempts table must exist after migration 012"


def test_journal_attempts_columns(db: sqlite3.Connection):
    """All specified columns are present with the expected names."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(journal_attempts)").fetchall()}
    expected = {
        "key",
        "task_id",
        "attempt",
        "persona",
        "phase",
        "model",
        "briefing_sha",
        "upstream_digest",
        "envelope_json",
        "usage_json",
        "created_at",
    }
    missing = expected - cols
    assert not missing, f"journal_attempts missing columns: {missing}"


def test_journal_attempts_key_is_primary_key(db: sqlite3.Connection):
    """``key`` column must be declared PRIMARY KEY (enforces uniqueness)."""
    pk_cols = [
        row[1]
        for row in db.execute("PRAGMA table_info(journal_attempts)").fetchall()
        if row[5] == 1  # pk column index
    ]
    assert pk_cols == ["key"], f"expected primary key on 'key', got: {pk_cols}"


# ── row round-trip ────────────────────────────────────────────────────────


def test_insert_and_select_round_trip(db: sqlite3.Connection):
    """Basic INSERT / SELECT works with all columns."""
    db.execute(
        "INSERT INTO journal_attempts "
        "(key, task_id, attempt, persona, phase, model, briefing_sha, upstream_digest, "
        "envelope_json, usage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "abc123",
            "T1",
            1,
            "backend-engineer-1",
            "implement",
            "claude-sonnet-4-5",
            "sha256_of_briefing",
            "upstream_digest_hex",
            '{"task_id":"T1","status":"done"}',
            '{"output_tokens":42}',
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM journal_attempts WHERE key=?", ("abc123",)).fetchone()
    assert row is not None
    assert row["task_id"] == "T1"
    assert row["attempt"] == 1
    assert row["persona"] == "backend-engineer-1"
    assert row["model"] == "claude-sonnet-4-5"
    assert row["envelope_json"] == '{"task_id":"T1","status":"done"}'
    assert row["usage_json"] == '{"output_tokens":42}'


def test_created_at_default_is_set(db: sqlite3.Connection):
    """created_at must be populated automatically via DEFAULT (datetime('now'))."""
    db.execute(
        "INSERT INTO journal_attempts (key, task_id) VALUES (?, ?)",
        ("key-default-test", "T-default"),
    )
    db.commit()
    row = db.execute(
        "SELECT created_at FROM journal_attempts WHERE key=?", ("key-default-test",)
    ).fetchone()
    assert row is not None
    created_at = row["created_at"]
    assert created_at is not None and created_at != "", (
        f"created_at must be auto-populated; got: {created_at!r}"
    )


def test_primary_key_uniqueness_enforced(db: sqlite3.Connection):
    """Inserting a duplicate key must raise IntegrityError."""
    db.execute(
        "INSERT INTO journal_attempts (key, task_id) VALUES (?, ?)",
        ("duplicate-key", "T1"),
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO journal_attempts (key, task_id) VALUES (?, ?)",
            ("duplicate-key", "T2"),
        )


# ── idempotency ───────────────────────────────────────────────────────────


def test_migration_012_idempotent(tmp_path: Path):
    """Applying shared/ twice must not raise or duplicate migration rows."""
    db_path = str(tmp_path / "idempotent.db")
    apply_migrations(db_path, SHARED)
    apply_migrations(db_path, SHARED)  # second run must no-op
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM migrations WHERE filename=?",
        ("012_result_journal.sql",),
    ).fetchone()[0]
    assert count == 1, f"migration 012 must appear exactly once in bookkeeping; got {count}"

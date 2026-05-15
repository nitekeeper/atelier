import sqlite3
import tempfile
import pytest
from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.db import get_connection

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

def test_all_tables_created(tmp_path):
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    conn = get_connection(db_path)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()}
    conn.close()
    expected = {"roles", "agents", "projects", "project_documents",
                "tasks", "meeting_minutes", "meeting_participants", "migrations",
                "sessions", "phases", "phase_transitions", "skill_gates",
                "phase_bypasses"}
    assert expected == tables

def test_migration_is_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    apply_migrations(db_path, MIGRATIONS_DIR)  # second run must not raise
    conn = get_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    conn.close()
    assert count == 5  # five migration files applied once each

def test_migration_recorded(tmp_path):
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    conn = get_connection(db_path)
    row = conn.execute("SELECT filename FROM migrations").fetchone()
    conn.close()
    assert row[0] == "001_initial_schema.sql"


def test_migration_005_creates_phase_bypasses_table(tmp_path):
    """phase_bypasses table exists after running migrations through 005."""
    db_path = str(tmp_path / "memex.db")
    apply_migrations(db_path, MIGRATIONS_DIR)

    conn = get_connection(db_path)

    # Table exists
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='phase_bypasses'"
    ).fetchone()
    assert row is not None, "phase_bypasses table not created"

    # Required columns present
    cols = {r[1] for r in conn.execute("PRAGMA table_info(phase_bypasses)").fetchall()}
    expected = {"id", "project_id", "skill", "current_phase", "required_phase",
                "bypassed_at", "agent_id", "note"}
    assert expected.issubset(cols), f"missing columns: {expected - cols}"

    # Index present
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='phase_bypasses_project_idx'"
    ).fetchone()
    assert idx is not None, "phase_bypasses_project_idx not created"

    conn.close()


def test_migration_005_is_idempotent(tmp_path):
    """Re-running migration 005 on an already-migrated DB is safe."""
    db_path = str(tmp_path / "memex.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    apply_migrations(db_path, MIGRATIONS_DIR)  # second run — must not raise

    conn = get_connection(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='phase_bypasses'"
    ).fetchone()[0]
    conn.close()
    assert count == 1

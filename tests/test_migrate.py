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
                "sessions", "phases", "phase_transitions", "skill_gates"}
    assert expected == tables

def test_migration_is_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    apply_migrations(db_path, MIGRATIONS_DIR)  # second run must not raise
    conn = get_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    conn.close()
    assert count == 4  # four migration files applied once each

def test_migration_recorded(tmp_path):
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    conn = get_connection(db_path)
    row = conn.execute("SELECT filename FROM migrations").fetchone()
    conn.close()
    assert row[0] == "001_initial_schema.sql"

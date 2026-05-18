"""
Plan 1 / Task 5 — migration layout tests.

v1.1.0 ships a clean schema, NOT additive migrations layered on v1.0.13.
This module locks the contract from spec §11.1 / §11.2:

  * `migrations/shared/` holds exactly one file: `001_v110_schema.sql` with
    the full DDL + the verbatim 19-phase seed + `index_id` columns inline.
  * `migrations/local-only/` holds `050_local_roles_agents.sql` so Local
    mode can boot without a `~/.memex/agents.db` to defer to.
  * No v1.0.13 migration files survive (`001_initial_schema.sql` … `005_soft_walls.sql`).
"""
import sqlite3
from pathlib import Path

import pytest

from scripts.migrate import apply_migrations

MIGRATIONS = Path(__file__).parent.parent / "migrations"


def test_shared_directory_exists():
    assert (MIGRATIONS / "shared").is_dir()


def test_local_only_directory_exists():
    assert (MIGRATIONS / "local-only").is_dir()


def test_v1_migrations_deleted():
    """v1.0.13 migrations are deleted per spec §11.1 — only the v1.1.0
    single-file migration remains under shared/."""
    for legacy in ("001_initial_schema.sql", "002_sessions.sql",
                   "003_phases.sql", "004_tasks_parallel.sql",
                   "005_soft_walls.sql"):
        assert not (MIGRATIONS / legacy).exists(), \
            f"v1.0.13 migration {legacy} must be deleted"
        assert not (MIGRATIONS / "shared" / legacy).exists(), \
            f"v1.0.13 migration {legacy} must not be moved into shared/"


def test_shared_has_single_v110_schema_file():
    files = sorted((MIGRATIONS / "shared").glob("*.sql"))
    assert len(files) == 1, \
        f"shared/ should hold exactly one file (001_v110_schema.sql), got {[f.name for f in files]}"
    assert files[0].name == "001_v110_schema.sql"


def test_shared_migration_does_not_define_roles_or_agents():
    f = MIGRATIONS / "shared" / "001_v110_schema.sql"
    text = f.read_text(encoding="utf-8")
    assert "CREATE TABLE roles" not in text and "CREATE TABLE IF NOT EXISTS roles" not in text, \
        "shared schema must not define roles table (lives in agents.db / local-only)"
    assert "CREATE TABLE agents" not in text and "CREATE TABLE IF NOT EXISTS agents" not in text, \
        "shared schema must not define agents table"


def test_local_only_migration_defines_roles_and_agents():
    f = MIGRATIONS / "local-only" / "050_local_roles_agents.sql"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "CREATE TABLE roles" in text
    assert "CREATE TABLE agents" in text


def test_apply_shared_only_to_fresh_db(tmp_path):
    """Memex-mode bootstrap supplies only shared/."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "roles" not in tables
    assert "agents" not in tables
    assert "workspaces" in tables
    assert "projects" in tables
    assert "tasks" in tables
    assert "sessions" in tables
    assert "phases" in tables
    assert "meeting_minutes" in tables


def test_apply_shared_then_local_to_fresh_db(tmp_path):
    """Local-mode bootstrap supplies both directories in order."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "roles" in tables
    assert "agents" in tables
    assert "projects" in tables


def test_apply_migrations_is_idempotent(tmp_path):
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "shared")  # second call must no-op
    con = sqlite3.connect(str(db))
    applied = con.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    assert applied == 1  # exactly one shared migration


def test_index_id_columns_present_in_shared_schema(tmp_path):
    """index_id columns are inline in 001_v110_schema.sql (no separate
    006 migration). Locks the spec §11.2 contract."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    for table in ("projects", "project_documents", "meeting_minutes", "tasks"):
        cols = [r[1] for r in con.execute(
            f"PRAGMA table_info({table})").fetchall()]
        assert "index_id" in cols, f"{table} missing index_id column"


def test_phases_seeded_from_shared(tmp_path):
    """The phase seed is inlined in 001_v110_schema.sql; a fresh DB has
    the full catalog after shared/ is applied."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    count = con.execute("SELECT COUNT(*) FROM phases").fetchone()[0]
    assert count >= 6, f"expected at least 6 phases seeded inline, got {count}"

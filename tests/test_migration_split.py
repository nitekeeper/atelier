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
import hashlib
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.migrate import apply_migrations

MIGRATIONS = Path(__file__).parent.parent / "migrations"
REPO_ROOT = Path(__file__).parent.parent


def test_shared_directory_exists():
    assert (MIGRATIONS / "shared").is_dir()


def test_local_only_directory_exists():
    assert (MIGRATIONS / "local-only").is_dir()


def test_v1_migrations_deleted():
    """v1.0.13 migrations are deleted per spec §11.1 — only the v1.1.0
    single-file migration remains under shared/. Local-only/ must also
    not house any of the v1 filenames."""
    legacy_files = (
        "001_initial_schema.sql",
        "002_sessions.sql",
        "003_phases.sql",
        "004_tasks_parallel.sql",
        "005_soft_walls.sql",
    )
    for legacy in legacy_files:
        assert not (MIGRATIONS / legacy).exists(), \
            f"v1.0.13 migration {legacy} must be deleted"
        assert not (MIGRATIONS / "shared" / legacy).exists(), \
            f"v1.0.13 migration {legacy} must not be moved into shared/"
        assert not (MIGRATIONS / "local-only" / legacy).exists(), \
            f"v1.0.13 migration {legacy} must not be moved into local-only/"


def test_shared_has_single_v110_schema_file():
    files = sorted((MIGRATIONS / "shared").glob("*.sql"))
    assert len(files) == 1, \
        f"shared/ should hold exactly one file (001_v110_schema.sql), got {[f.name for f in files]}"
    assert files[0].name == "001_v110_schema.sql"


def test_apply_shared_only_creates_no_roles_or_agents(tmp_path):
    """Memex-mode bootstrap supplies only shared/; roles+agents live in
    Memex's agents.db, not in the shared schema. Post-apply structural
    check is tighter than a string match on the SQL source."""
    db = tmp_path / "test.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    with sqlite3.connect(str(db)) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "roles" not in tables
    assert "agents" not in tables


def test_local_only_migration_defines_roles_and_agents():
    f = MIGRATIONS / "local-only" / "050_local_roles_agents.sql"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "CREATE TABLE roles" in text
    assert "CREATE TABLE agents" in text


def test_memex_mode_bootstrap_shared_only(tmp_path):
    """Memex-mode bootstrap supplies only shared/. All 12 v1.1.0 tables
    should be present except roles/agents (which live in agents.db).
    Renamed from test_apply_shared_only_to_fresh_db per QA Nit-6."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "roles" not in tables
    assert "agents" not in tables
    # All 12 v1.1.0 tables in shared/ — pinned per QA Nit-3.
    expected_shared_tables = {
        "workspaces",
        "projects",
        "project_documents",
        "tasks",
        "meeting_minutes",
        "meeting_participants",
        "sessions",
        "phases",
        "phase_transitions",
        "skill_gates",
        "phase_bypasses",
        "migrations",  # bookkeeping table created by apply_migrations
    }
    missing = expected_shared_tables - tables
    assert not missing, f"shared/ missing tables: {missing}"


def test_local_mode_bootstrap_shared_plus_local(tmp_path):
    """Local-mode bootstrap supplies both directories in order.
    Renamed from test_apply_shared_then_local_to_fresh_db per QA Nit-6."""
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


def test_idempotency_of_shared_plus_local(tmp_path):
    """Re-applying both shared/ and local-only/ must remain idempotent —
    exactly 2 rows in `migrations`, one per file. Per QA Nit-5."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    apply_migrations(str(db), MIGRATIONS / "shared")  # re-apply
    apply_migrations(str(db), MIGRATIONS / "local-only")  # re-apply
    with sqlite3.connect(str(db)) as con:
        count = con.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    assert count == 2, f"expected exactly 2 migration rows, got {count}"


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


def test_index_id_indexes_present_in_shared_schema(tmp_path):
    """Each `index_id` column has a supporting B-tree index. Per QA Imp-3."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    with sqlite3.connect(str(db)) as con:
        names = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%index_id%'"
        )}
    expected = {
        "idx_projects_index_id",
        "idx_docs_index_id",
        "idx_tasks_index_id",
        "idx_meetings_index_id",
    }
    assert expected <= names, f"missing index_id indexes: {expected - names}"


def test_phases_seeded_from_shared(tmp_path):
    """The phase seed is inlined in 001_v110_schema.sql; a fresh DB has
    the full catalog after shared/ is applied. Pin exact counts per
    QA Imp-1 — reviewer confirmed 33 transitions (not 32)."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    with sqlite3.connect(str(db)) as con:
        assert con.execute("SELECT COUNT(*) FROM phases").fetchone()[0] == 19
        assert con.execute("SELECT COUNT(*) FROM phase_transitions").fetchone()[0] == 33
        assert con.execute("SELECT COUNT(*) FROM skill_gates").fetchone()[0] == 8


def test_phase_seed_sha256_pinned():
    """Verbatim parity with v1.0.13's 003_phases.sql; updates require
    explicit hash bump. Per QA Nit-2 — guards against silent drift of
    the inlined seed block."""
    text = (MIGRATIONS / "shared" / "001_v110_schema.sql").read_text(encoding="utf-8")
    block = "\n".join(re.findall(
        r"INSERT OR IGNORE INTO (?:phases|phase_transitions|skill_gates).*?;",
        text,
        re.DOTALL,
    ))
    actual = hashlib.sha256(block.encode("utf-8")).hexdigest()
    expected = "48bd3ce5791f2c231df171c4e389a9436fa53706fdfaf39b6b89683b9c6a1043"
    assert actual == expected, (
        f"Phase seed bytes drifted (sha256={actual}); update hash AND "
        "bump v1.1.0 → v1.2.0 if intentional."
    )


def test_foreign_keys_enforced_in_get_connection(tmp_path):
    """scripts/db.py.get_connection enables FK enforcement per spec §11.2.
    The shared schema does NOT set PRAGMA foreign_keys (connection-scoped);
    enforcement is therefore a runtime contract of get_connection().
    Per QA Imp-2."""
    from scripts.db import get_connection
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    con = get_connection(str(db))
    try:
        # Verify pragma is on.
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # Verify behavior: inserting child row referencing a non-existent
        # parent must raise IntegrityError.
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO projects "
                "(workspace_id, slug, name, description, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (99999, "x", "X", "d", "agent", "2026-05-18T00:00:00Z", "2026-05-18T00:00:00Z"),
            )
    finally:
        con.close()


def test_migrate_cli_applies_both_dirs(tmp_path):
    """Reviewer C1: the `python scripts/migrate.py <db>` entrypoint must
    apply BOTH shared/ and local-only/ (Local-mode default). Pre-fix
    this was a no-op because `migrations/` is a flat dir of subdirs."""
    db = tmp_path / "cli.db"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "migrate.py"), str(db)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": ""},
        check=True,
    )
    assert "Migrations applied" in result.stdout
    with sqlite3.connect(str(db)) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        applied = {r[0] for r in con.execute("SELECT filename FROM migrations")}
    # shared/ landed: workspaces table is present.
    assert "workspaces" in tables, "CLI did not apply shared/"
    # local-only/ landed: roles + agents tables are present.
    assert "roles" in tables, "CLI did not apply local-only/"
    assert "agents" in tables, "CLI did not apply local-only/"
    # Both migration filenames recorded.
    assert "001_v110_schema.sql" in applied
    assert "050_local_roles_agents.sql" in applied

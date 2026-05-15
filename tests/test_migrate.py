from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.db import get_connection

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

def test_all_tables_created(tmp_path):
    """All expected tables exist after running the full migration set."""
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
    """Running migrations twice does not duplicate records or raise errors."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    apply_migrations(db_path, MIGRATIONS_DIR)  # second run must not raise
    conn = get_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    conn.close()
    assert count == 5  # five migration files applied once each

def test_migration_recorded(tmp_path):
    """The first migration filename is recorded in the migrations bookkeeping table."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    conn = get_connection(db_path)
    row = conn.execute("SELECT filename FROM migrations").fetchone()
    conn.close()
    assert row[0] == "001_initial_schema.sql"


def test_migration_005_creates_phase_bypasses_table(tmp_path):
    """phase_bypasses table exists after running migrations through 005."""
    db_path = str(tmp_path / "test.db")
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
    assert expected == cols, f"column mismatch — extra: {cols - expected}, missing: {expected - cols}"

    # Index present
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='phase_bypasses_project_idx'"
    ).fetchone()
    assert idx is not None, "phase_bypasses_project_idx not created"

    conn.close()


def test_migration_005_is_idempotent(tmp_path):
    """Re-running migration 005 on an already-migrated DB is safe."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    apply_migrations(db_path, MIGRATIONS_DIR)  # second run — must not raise

    conn = get_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    conn.close()
    assert count == 5, "migrations were double-applied or skipped on second run"


def _insert_role_and_agent(conn, agent_id: str, agent_name: str) -> int:
    """Helper: insert a role and agent, return the role id."""
    conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) "
        "VALUES ('test-role', 'Test role', datetime('now'), datetime('now'))"
    )
    role_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, created_at, updated_at) "
        "VALUES (?, ?, ?, 'Test profile', datetime('now'), datetime('now'))",
        (agent_id, agent_name, role_id),
    )
    return role_id


def test_migration_005_project_delete_cascades_to_bypasses(tmp_path):
    """Deleting a project removes its phase_bypasses rows via ON DELETE CASCADE."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)

    conn = get_connection(db_path)
    # get_connection already sets PRAGMA foreign_keys=ON

    _insert_role_and_agent(conn, "agent-cascade", "Cascade Agent")
    conn.execute(
        "INSERT INTO projects (id, name, phase, created_by, created_at, updated_at) "
        "VALUES (9001, 'Cascade Project', 'design:open', 'agent-cascade', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO phase_bypasses (project_id, skill, current_phase, required_phase) "
        "VALUES (9001, 'dev:plan', 'design:open', 'design:approved')"
    )
    conn.commit()

    bypass_before = conn.execute(
        "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = 9001"
    ).fetchone()[0]
    assert bypass_before == 1, "setup failed: bypass row not inserted"

    conn.execute("DELETE FROM projects WHERE id = 9001")
    conn.commit()

    bypass_after = conn.execute(
        "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = 9001"
    ).fetchone()[0]
    conn.close()
    assert bypass_after == 0, "ON DELETE CASCADE did not remove phase_bypasses rows"


def test_migration_005_agent_delete_nulls_agent_id_in_bypasses(tmp_path):
    """Deleting an agent sets agent_id to NULL in phase_bypasses via ON DELETE SET NULL."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)

    conn = get_connection(db_path)
    # get_connection already sets PRAGMA foreign_keys=ON

    # Insert agent, project, and a bypass row referencing the agent
    _insert_role_and_agent(conn, "agent-setnull", "SetNull Agent")
    conn.execute(
        "INSERT INTO projects (id, name, phase, created_by, created_at, updated_at) "
        "VALUES (9002, 'SetNull Project', 'design:open', 'agent-setnull', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO phase_bypasses (project_id, skill, current_phase, required_phase, agent_id) "
        "VALUES (9002, 'dev:plan', 'design:open', 'design:approved', 'agent-setnull')"
    )
    conn.commit()

    bypass_id = conn.execute(
        "SELECT id FROM phase_bypasses WHERE project_id = 9002"
    ).fetchone()[0]

    # Deleting the agent that created the project would violate the projects FK;
    # insert a replacement agent and update the project's created_by first.
    _insert_role_and_agent(conn, "agent-owner", "Owner Agent")
    conn.execute("UPDATE projects SET created_by = 'agent-owner' WHERE id = 9002")
    conn.commit()

    conn.execute("DELETE FROM agents WHERE id = 'agent-setnull'")
    conn.commit()

    row = conn.execute(
        "SELECT agent_id FROM phase_bypasses WHERE id = ?", (bypass_id,)
    ).fetchone()
    conn.close()
    assert row is not None, "bypass row was unexpectedly deleted"
    assert row[0] is None, f"expected agent_id to be NULL after agent delete, got: {row[0]!r}"

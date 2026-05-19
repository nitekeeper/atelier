"""Plan 3 Task 9 — rewritten for the v1.1.0 shared/local-only migration split.

`scripts/db.py` was retired (Plan 3 Task 9). `scripts/migrate.py` owns the
SQLite connection helper inline; consumers that need a handle either import
that helper directly (rare) or route through the `scripts/backend.py`
facade. These tests cover only the migration runner itself.

Layout pinned here:

* `migrations/shared/001_v110_schema.sql` — v1.1.0 base schema (12 tables
  including `phase_bypasses` with `from_phase / to_phase / reason /
  agent_id / created_at`).
* `migrations/shared/` may additionally house append-only follow-ups in
  the 002-049 band (e.g., `002_source_ref_and_fts.sql` shipped with
  Plan 2). The naming convention reserves 050+ for local-only/.
* `migrations/local-only/050_local_roles_agents.sql` — Local-mode-only
  `roles` + `agents` tables (Memex mode defers to `~/.memex/agents.db`).
"""

from contextlib import closing
from pathlib import Path

from scripts.migrate import apply_migrations, get_connection

MIGRATIONS_DIR: Path = Path(__file__).parent.parent / "migrations"
SHARED = MIGRATIONS_DIR / "shared"
LOCAL_ONLY = MIGRATIONS_DIR / "local-only"


# ── Layout invariants ──────────────────────────────────────────────────────


def test_shared_contains_v110_base_schema():
    """`shared/` houses `001_v110_schema.sql` as its base file (the
    canonical v1.1.0 bootstrap). Follow-up migrations may be added in
    the 002-049 band — this test pins only the base slot."""
    files = sorted(SHARED.glob("*.sql"))
    names = [f.name for f in files]
    assert "001_v110_schema.sql" in names, f"shared/ must contain 001_v110_schema.sql; got {names}"
    assert names[0] == "001_v110_schema.sql", (
        f"shared/ first-by-name file must be 001_v110_schema.sql; got {names[0]}"
    )
    # Convention: 001-049 reserved for shared/, 050+ for local-only/.
    for name in names:
        prefix = name.split("_", 1)[0]
        assert prefix.isdigit() and 1 <= int(prefix) <= 49, (
            f"shared/ file {name} outside the 001-049 band"
        )


def test_local_only_has_exactly_one_migration():
    """`local-only/` ships exactly one bootstrap migration —
    `050_local_roles_agents.sql` — in the 050+ band."""
    files = sorted(LOCAL_ONLY.glob("*.sql"))
    assert [f.name for f in files] == ["050_local_roles_agents.sql"], (
        f"local-only/ must contain exactly 050_local_roles_agents.sql; "
        f"got {[f.name for f in files]}"
    )


# ── Runner contract ────────────────────────────────────────────────────────


def test_apply_shared_then_local_creates_all_tables(tmp_path):
    """Local-mode bootstrap applies shared/ then local-only/ — the union
    contains every v1.1.0 base table including roles + agents. FTS5
    shadow tables created by `shared/002_source_ref_and_fts.sql` (the
    `project_documents_fts*` family) are tolerated as auxiliary."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, SHARED)
    apply_migrations(db_path, LOCAL_ONLY)
    with closing(get_connection(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name NOT LIKE 'project_documents_fts%'"
            ).fetchall()
        }
    expected = {
        # shared/ tables
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
        "migrations",  # bookkeeping
        # local-only/ tables
        "roles",
        "agents",
    }
    assert expected == tables, (
        f"tables mismatch — extra: {tables - expected}, missing: {expected - tables}"
    )


def test_migration_recorded_with_v110_filenames(tmp_path):
    """Pin the two canonical v1.1.0 filenames in the `migrations`
    bookkeeping table after a full bootstrap. Follow-up shared
    migrations (002-049) may also be present; we only assert the
    canonical two are recorded — exhaustive layout coverage lives in
    `tests/test_migration_split.py`."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, SHARED)
    apply_migrations(db_path, LOCAL_ONLY)
    with closing(get_connection(db_path)) as conn:
        filenames = {row[0] for row in conn.execute("SELECT filename FROM migrations").fetchall()}
    assert "001_v110_schema.sql" in filenames
    assert "050_local_roles_agents.sql" in filenames


def test_apply_shared_then_local_is_idempotent(tmp_path):
    """Re-applying both directories does not duplicate records or raise.
    Row count equals total *.sql files across both dirs (1 per file)."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, SHARED)
    apply_migrations(db_path, LOCAL_ONLY)
    apply_migrations(db_path, SHARED)  # second run — must not raise
    apply_migrations(db_path, LOCAL_ONLY)  # second run — must not raise
    expected = len(list(SHARED.glob("*.sql"))) + len(list(LOCAL_ONLY.glob("*.sql")))
    with closing(get_connection(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    assert count == expected, f"expected {expected} migration rows, got {count}"


# ── phase_bypasses shape (v1.1.0 column rename) ────────────────────────────


def test_phase_bypasses_v110_columns(tmp_path):
    """v1.1.0 renamed v1.0.13's `current_phase`/`required_phase` to
    `from_phase`/`to_phase`, added `reason`, kept `agent_id` (no FK per
    TODO Nit-3 — Memex-mode bypasses log against agents in
    `~/.memex/agents.db` which isn't visible to the workspace DB), and
    relies on `created_at DEFAULT (datetime('now'))`."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, SHARED)
    with closing(get_connection(db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(phase_bypasses)").fetchall()}
    expected = {"id", "project_id", "from_phase", "to_phase", "reason", "agent_id", "created_at"}
    assert expected == cols, (
        f"phase_bypasses column mismatch — extra: {cols - expected}, missing: {expected - cols}"
    )


def test_phase_bypasses_required_nonnull_columns(tmp_path):
    """`project_id`, `from_phase`, `to_phase`, `reason`, `agent_id`, and
    `created_at` are NOT NULL per spec §11.2 — the bypass audit trail
    must be complete. `id` is `INTEGER PRIMARY KEY AUTOINCREMENT` and
    therefore reports `notnull=0` in PRAGMA output (SQLite assigns it
    automatically); excluded from the assertion."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, SHARED)
    with closing(get_connection(db_path)) as conn:
        notnull_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(phase_bypasses)").fetchall() if r[3] == 1
        }  # column 3 is `notnull` in PRAGMA output
    expected_notnull = {"project_id", "from_phase", "to_phase", "reason", "agent_id", "created_at"}
    assert expected_notnull <= notnull_cols, (
        f"phase_bypasses NOT NULL constraint missing on: {expected_notnull - notnull_cols}"
    )


def test_phase_bypasses_insert_accepts_v110_shape(tmp_path):
    """End-to-end smoke: a v1.1.0-shaped INSERT into `phase_bypasses`
    succeeds (covers a workspace + project + agent dependency chain so
    the FK to projects is satisfied)."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, SHARED)
    apply_migrations(db_path, LOCAL_ONLY)
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO workspaces (slug, identity, name, created_at, updated_at) "
            "VALUES ('w', 'identity-1', 'W', datetime('now'), datetime('now'))"
        )
        ws_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO roles (name, description, created_at, updated_at) "
            "VALUES ('test-role', 'd', datetime('now'), datetime('now'))"
        )
        role_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO agents (id, name, role_id, profile, created_at, updated_at) "
            "VALUES ('agent-1', 'Agent One', ?, 'p', datetime('now'), datetime('now'))",
            (role_id,),
        )
        conn.execute(
            "INSERT INTO projects (workspace_id, slug, name, created_by, created_at, updated_at) "
            "VALUES (?, 'proj', 'Proj', 'agent-1', datetime('now'), datetime('now'))",
            (ws_id,),
        )
        proj_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO phase_bypasses "
            "(project_id, from_phase, to_phase, reason, agent_id) "
            "VALUES (?, 'design:open', 'plan:open', 'fast-track', 'agent-1')",
            (proj_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT project_id, from_phase, to_phase, reason, agent_id "
            "FROM phase_bypasses WHERE project_id = ?",
            (proj_id,),
        ).fetchone()
    assert row == (proj_id, "design:open", "plan:open", "fast-track", "agent-1")


# ── get_connection contract (inlined from former scripts/db.py) ────────────


def test_get_connection_enables_wal(tmp_path):
    """`scripts/migrate.py.get_connection` enables WAL on every handle —
    inlined from the retired `scripts/db.py` so the runner remains
    self-contained."""
    db_path = str(tmp_path / "test.db")
    with closing(get_connection(db_path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_get_connection_enables_foreign_keys(tmp_path):
    """FK enforcement is connection-scoped per spec §11.2; the helper
    sets it on every open."""
    db_path = str(tmp_path / "test.db")
    with closing(get_connection(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

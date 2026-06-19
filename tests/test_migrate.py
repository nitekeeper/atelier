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

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

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
        # shared/ tables (001)
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
        # shared/ tables (003 — team-mode foundationals; epic #37)
        "teams",
        "persona_snapshots",
        "team_members",
        "bridge_messages",
        "bridge_delivery",
        "shutdown_requests",
        "team_audit_log",
        # shared/ tables (011 — content-addressed out-of-band payload store)
        "bridge_payloads",
        # shared/ tables (012 — deterministic host engine result journal)
        "journal_attempts",
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


# ── Runner reconcile guard (ledger behind, schema ahead) ───────────────────


def _make_migrations(tmp_path: Path) -> Path:
    """A throwaway migrations dir with one base-schema file."""
    d = tmp_path / "migs"
    d.mkdir()
    (d / "001_base.sql").write_text(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT);\n"
        "CREATE INDEX idx_widgets_name ON widgets(name);\n"
    )
    return d


def test_fresh_store_applies_cleanly(tmp_path):
    """A fresh store applies every file and records each exactly once."""
    db_path = str(tmp_path / "fresh.db")
    migs = _make_migrations(tmp_path)
    apply_migrations(db_path, migs)
    with closing(get_connection(db_path)) as conn:
        recorded = {r[0] for r in conn.execute("SELECT filename FROM migrations")}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert recorded == {"001_base.sql"}
    assert "widgets" in tables


def test_fully_applied_store_is_noop(tmp_path):
    """Re-running on a fully-applied store records nothing new and does not
    raise (the filename gate short-circuits before any SQL runs)."""
    db_path = str(tmp_path / "noop.db")
    migs = _make_migrations(tmp_path)
    apply_migrations(db_path, migs)
    apply_migrations(db_path, migs)  # must not raise
    with closing(get_connection(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    assert count == 1


def test_desynced_store_reconciles_without_crash(tmp_path):
    """Ledger behind, schema ahead → reconcile.

    Simulate the desync: the schema object (`widgets` + its index) already
    exists, but the `migrations` ledger has NO row for the file. A naive
    runner re-runs the migration and crashes on "already exists". The guarded
    runner must treat it as ALREADY-APPLIED: record the filename and continue.
    """
    db_path = str(tmp_path / "desync.db")
    migs = _make_migrations(tmp_path)
    # Build the schema object directly, WITHOUT recording it in the ledger.
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "CREATE TABLE migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "filename TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE INDEX idx_widgets_name ON widgets(name)")
        conn.execute("INSERT INTO widgets (name) VALUES ('keep-me')")
        conn.commit()

    # Must NOT raise — reconciles instead.
    apply_migrations(db_path, migs)

    with closing(get_connection(db_path)) as conn:
        recorded = {r[0] for r in conn.execute("SELECT filename FROM migrations")}
        # Data preserved — reconcile never touched the existing rows.
        names = [r[0] for r in conn.execute("SELECT name FROM widgets")]
    assert recorded == {"001_base.sql"}, "reconcile must record the filename"
    assert names == ["keep-me"], "reconcile must not destroy existing data"


def test_genuine_error_still_propagates(tmp_path):
    """A non-'already exists' error MUST propagate (no blanket except)."""
    db_path = str(tmp_path / "broken.db")
    migs = tmp_path / "broken_migs"
    migs.mkdir()
    # References a table that does not exist → "no such table" OperationalError,
    # which is NOT in the already-exists family and must propagate.
    (migs / "001_broken.sql").write_text("INSERT INTO does_not_exist (x) VALUES (1);\n")
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(db_path, migs)
    # The failed migration must NOT be recorded.
    with closing(get_connection(db_path)) as conn:
        recorded = {r[0] for r in conn.execute("SELECT filename FROM migrations")}
    assert "001_broken.sql" not in recorded


def test_mixed_file_new_statement_after_collision_still_runs(tmp_path):
    """CRITICAL regression — the mixed-file / partial-presence hazard.

    A not-yet-recorded file whose FIRST statement is already-present but whose
    LATER statements are genuinely new. A whole-file `executescript` reconcile
    would abort at statement 1 and record the file as applied, SILENTLY SKIPPING
    every later statement forever. Per-statement reconcile must skip only the
    collided statement and still apply the new ones, recording the file only
    after ALL statements are processed.
    """
    db_path = str(tmp_path / "mixed.db")
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "CREATE TABLE migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "filename TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
        )
        # `early` already exists (partial prior run); `late` does NOT.
        conn.execute("CREATE TABLE early (x)")
        conn.commit()
    migs = tmp_path / "mixed_migs"
    migs.mkdir()
    (migs / "001_mixed.sql").write_text(
        "CREATE TABLE early (x);\n"  # collides → skipped
        "CREATE TABLE late (y);\n"  # genuinely new → MUST still run
    )
    apply_migrations(db_path, migs)  # must not raise
    with closing(get_connection(db_path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        recorded = {r[0] for r in conn.execute("SELECT filename FROM migrations")}
    assert "late" in tables, "statement AFTER a collision must still execute"
    assert recorded == {"001_mixed.sql"}, "file recorded once fully processed"


def test_partial_002_completes_on_rerun(tmp_path):
    """Realistic partial-`002` variant on the REAL migration files.

    Simulate a crash after 002's first `ALTER TABLE project_documents ADD COLUMN
    source_ref` (autocommitted) with 002 NOT yet ledger-recorded. The guarded
    re-run hits `duplicate column name: source_ref` on statement 1 and must
    still create the LATER 002 objects: tasks.source_ref, meeting_minutes
    .source_ref, the FTS table, and the 3 sync triggers.
    """
    migrations = Path(__file__).parent.parent / "migrations"
    shared = migrations / "shared"
    db_path = str(tmp_path / "partial002.db")

    # Apply only 001 (record it), then hand-apply 002's first statement so the
    # column is present but 002 is unrecorded — the exact desync.
    only_001 = tmp_path / "only_001"
    only_001.mkdir()
    (only_001 / "001_v110_schema.sql").write_text((shared / "001_v110_schema.sql").read_text())
    apply_migrations(db_path, only_001)
    with closing(get_connection(db_path)) as conn:
        conn.execute("ALTER TABLE project_documents ADD COLUMN source_ref TEXT")
        conn.commit()

    # Full shared run: 002 reconciles per-statement; later objects still build.
    apply_migrations(db_path, shared)

    with closing(get_connection(db_path)) as conn:
        tasks_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        mtg_cols = {r[1] for r in conn.execute("PRAGMA table_info(meeting_minutes)")}
        fts = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='project_documents_fts'"
        ).fetchone()[0]
        trigs = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'project_documents_%'"
            )
        }
        recorded = {r[0] for r in conn.execute("SELECT filename FROM migrations")}
    assert "source_ref" in tasks_cols, "tasks.source_ref (post-collision stmt) must exist"
    assert "source_ref" in mtg_cols, "meeting_minutes.source_ref must exist"
    assert fts == 1, "project_documents_fts must be created"
    assert trigs == {
        "project_documents_ai",
        "project_documents_ad",
        "project_documents_au",
    }, f"all 3 FTS triggers must exist; got {trigs}"
    assert "002_source_ref_and_fts.sql" in recorded


def test_wrapped_file_midfile_failure_rolls_back(tmp_path):
    """A BEGIN-wrapped file that hits a genuine (non-already-exists) error
    mid-file must roll the WHOLE file back and NOT be recorded — the error
    propagates, no half-applied state, no leaked open transaction."""
    db_path = str(tmp_path / "wrapped.db")
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "CREATE TABLE migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "filename TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('original')")
        conn.commit()
    migs = tmp_path / "wrapped_migs"
    migs.mkdir()
    # Wrapped: drops t, then a genuine error (missing table) inside the BEGIN
    # block. The whole block must roll back — t (and its row) intact.
    (migs / "001_wrapped.sql").write_text(
        "BEGIN;\nDROP TABLE t;\nINSERT INTO nonexistent_table VALUES (1);\nCOMMIT;\n"
    )
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(db_path, migs)
    with closing(get_connection(db_path)) as conn:
        rows = [r[0] for r in conn.execute("SELECT v FROM t")]
        recorded = {r[0] for r in conn.execute("SELECT filename FROM migrations")}
        leaked = conn.in_transaction
    assert rows == ["original"], "wrapped file must roll back fully on genuine error"
    assert "001_wrapped.sql" not in recorded, "failed file must NOT be recorded"
    assert not leaked, "no open transaction may leak"


def test_split_statements_handles_trigger_and_wrapped_begin():
    """`_split_statements` keeps a trigger BEGIN...END body as ONE statement
    (its inner `;` do not split) and emits self-wrapped `BEGIN;`/`COMMIT;` as
    their own statements (string literals + comments with `;` are respected)."""
    from scripts.migrate import _split_statements

    sql = (
        "-- a comment; with a semicolon\n"
        "ALTER TABLE pd ADD COLUMN x TEXT;\n"
        "CREATE TRIGGER tr AFTER INSERT ON pd BEGIN\n"
        "  INSERT INTO pd(x) VALUES('a;b');\n"
        "  INSERT INTO pd(x) VALUES('c');\n"
        "END;\n"
        "BEGIN;\n"
        "DROP TABLE pd;\n"
        "COMMIT;\n"
    )
    stmts = _split_statements(sql)
    assert len(stmts) == 5, f"expected 5 statements, got {len(stmts)}: {stmts}"
    assert stmts[0].endswith("ADD COLUMN x TEXT;")
    assert stmts[1].startswith("CREATE TRIGGER tr") and stmts[1].endswith("END;")
    assert "VALUES('a;b')" in stmts[1], "string-literal semicolon must stay in the trigger body"
    assert stmts[2] == "BEGIN;"
    assert stmts[3] == "DROP TABLE pd;"
    assert stmts[4] == "COMMIT;"


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

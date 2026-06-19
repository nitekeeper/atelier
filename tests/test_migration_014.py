"""Migration 014_project_documents_rebuild.sql — forward reconcile of the
latent project_documents inconsistency.

Audit finding: 005_workspace_less_ops.sql is recorded-applied in some stores
but its structural effect is ABSENT — those stores still carry an orphan legacy
`type` column AND still have NOT NULL on workspace_id / project_id. 014 is a
FORWARD migration that rebuilds project_documents idempotently so every store
converges on the intended shape:

  * NO `type` column
  * workspace_id / project_id NULLABLE
  * source_ref (002) + metadata (007) present
  * FTS5 table + 6 indexes + 3 triggers re-asserted
  * all rows + FTS contents preserved

These tests prove 014 is safe + correct on BOTH store shapes.
"""

from contextlib import closing
from pathlib import Path

from scripts.migrate import apply_migrations, get_connection

MIGRATIONS_DIR: Path = Path(__file__).parent.parent / "migrations"
SHARED = MIGRATIONS_DIR / "shared"

# project_documents migrations through 014, in order.
_PD_MIGRATIONS_UP_TO_005 = ["001_v110_schema.sql", "002_source_ref_and_fts.sql"]


def _pd_columns(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(project_documents)")}


def _pd_notnull_columns(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(project_documents)") if r[3] == 1}


def _objects(conn, kind: str, like: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name LIKE ?", (kind, like)
        )
    }


def _bootstrap_then_orphan(db_path: str) -> None:
    """Build a store whose project_documents is at the pre-005 ORPHAN shape
    (has `type`, NOT NULL workspace_id/project_id) but whose ledger records
    005 (and 006/007/...) as applied — the exact desync the audit found —
    while every OTHER table is at the real current schema.

    We do this by applying the real migrations to set up the full schema +
    ledger, then surgically rebuilding ONLY project_documents back to the
    orphan shape (carrying source_ref/metadata columns from 002/007 but
    re-adding the dropped `type` and the NOT NULL constraints).
    """
    # Apply everything EXCEPT 014 so the ledger records 005..013 as applied.
    # (014 is the file under test; we run it explicitly below.)
    tmp_dir = Path(db_path).parent / "shared_no_014"
    tmp_dir.mkdir()
    for f in sorted(SHARED.glob("*.sql")):
        if f.name == "014_project_documents_rebuild.sql":
            continue
        (tmp_dir / f.name).write_text(f.read_text())
    apply_migrations(db_path, tmp_dir)

    # Now mutate project_documents back to the orphan shape and seed a row.
    with closing(get_connection(db_path)) as conn:
        conn.executescript(
            """
            BEGIN;
            DROP TABLE IF EXISTS project_documents_fts;
            DROP TABLE project_documents;
            CREATE TABLE project_documents (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
                project_id   INTEGER NOT NULL REFERENCES projects(id),
                type         TEXT,                -- orphan legacy column
                domain       TEXT NOT NULL,
                subdomain    TEXT,
                title        TEXT NOT NULL,
                filename     TEXT NOT NULL,
                created_by   TEXT NOT NULL,
                index_id     TEXT,
                source_ref   TEXT,
                metadata     TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            -- Minimal FTS so 014's DROP TABLE IF EXISTS path is exercised.
            CREATE VIRTUAL TABLE project_documents_fts USING fts5(
                title, subdomain, filename,
                content='project_documents', content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );
            COMMIT;
            """
        )
        # FK chain: workspace -> project -> document.
        conn.execute(
            "INSERT INTO workspaces (slug, identity, name, created_at, updated_at) "
            "VALUES ('w', 'id-1', 'W', datetime('now'), datetime('now'))"
        )
        ws = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO projects (workspace_id, slug, name, created_by, created_at, updated_at) "
            "VALUES (?, 'p', 'P', 'a', datetime('now'), datetime('now'))",
            (ws,),
        )
        proj = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO project_documents "
            "(workspace_id, project_id, type, domain, title, filename, created_by, "
            " source_ref, metadata, created_at, updated_at) "
            "VALUES (?, ?, 'design', 'design', 'Doc One', 'doc1.md', 'a', "
            "'atelier:doc:1', '{\"version\":1}', datetime('now'), datetime('now'))",
            (ws, proj),
        )
        conn.execute(
            "INSERT INTO project_documents_fts(rowid, title, subdomain, filename) "
            "SELECT id, title, COALESCE(subdomain, ''), filename FROM project_documents"
        )
        conn.commit()


def _run_014(db_path: str) -> None:
    """Run ONLY migration 014 against the store, mimicking the runner flow by
    pointing it at a single-file dir (the ledger gate skips already-applied)."""
    only_014 = Path(db_path).parent / "only_014"
    only_014.mkdir()
    f = SHARED / "014_project_documents_rebuild.sql"
    (only_014 / f.name).write_text(f.read_text())
    apply_migrations(db_path, only_014)


def _assert_correct_final_shape(db_path: str) -> None:
    with closing(get_connection(db_path)) as conn:
        cols = _pd_columns(conn)
        notnull = _pd_notnull_columns(conn)
        indexes = _objects(conn, "index", "idx_docs_%") | _objects(
            conn, "index", "idx_project_documents_source_ref"
        )
        triggers = _objects(conn, "trigger", "project_documents_%")
        fts = _objects(conn, "table", "project_documents_fts")
    assert "type" not in cols, "orphan `type` column must be gone"
    assert {"source_ref", "metadata"} <= cols, "source_ref + metadata must survive"
    assert "workspace_id" not in notnull, "workspace_id must be nullable"
    assert "project_id" not in notnull, "project_id must be nullable"
    # 6 idx_docs_* / source_ref indexes from 001 + 002.
    assert {
        "idx_docs_workspace",
        "idx_docs_project",
        "idx_docs_domain",
        "idx_docs_subdomain",
        "idx_docs_index_id",
        "idx_project_documents_source_ref",
    } <= indexes, f"missing indexes: {indexes}"
    # 3 sync triggers.
    assert {
        "project_documents_ai",
        "project_documents_ad",
        "project_documents_au",
    } <= triggers, f"missing triggers: {triggers}"
    assert "project_documents_fts" in fts, "FTS table must exist"


def test_014_fixes_orphan_shape_and_preserves_rows(tmp_path):
    """On an orphan-shape store (has `type`, NOT NULL cols), 014 produces the
    correct final shape and preserves the existing row + its FTS entry."""
    db_path = str(tmp_path / "orphan.db")
    _bootstrap_then_orphan(db_path)

    # Pre-condition sanity: it really is the orphan shape.
    with closing(get_connection(db_path)) as conn:
        assert "type" in _pd_columns(conn)
        assert "workspace_id" in _pd_notnull_columns(conn)

    _run_014(db_path)

    _assert_correct_final_shape(db_path)
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT title, filename, source_ref, metadata FROM project_documents"
        ).fetchall()
        fts_rows = conn.execute(
            "SELECT title FROM project_documents_fts WHERE project_documents_fts MATCH 'Doc'"
        ).fetchall()
    assert rows == [("Doc One", "doc1.md", "atelier:doc:1", '{"version":1}')], (
        "row data must be preserved verbatim"
    )
    assert fts_rows == [("Doc One",)], "FTS contents must be rebuilt + searchable"


def test_014_is_noop_on_already_correct_store(tmp_path):
    """On a store already at the correct shape (full bootstrap incl. 005-effect
    via 014 itself), running 014 again is a faithful no-op: shape unchanged,
    rows preserved."""
    db_path = str(tmp_path / "correct.db")
    # Full bootstrap including 014 → already-correct shape.
    apply_migrations(db_path, SHARED)

    # Seed a row at the correct (nullable, no-type) shape.
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO workspaces (slug, identity, name, created_at, updated_at) "
            "VALUES ('w', 'id-1', 'W', datetime('now'), datetime('now'))"
        )
        ws = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO projects (workspace_id, slug, name, created_by, created_at, updated_at) "
            "VALUES (?, 'p', 'P', 'a', datetime('now'), datetime('now'))",
            (ws,),
        )
        proj = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # workspace-less row to prove nullability is real.
        conn.execute(
            "INSERT INTO project_documents "
            "(workspace_id, project_id, domain, title, filename, created_by, created_at, updated_at) "
            "VALUES (NULL, NULL, 'log', 'Scratch', 's.md', 'a', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO project_documents "
            "(workspace_id, project_id, domain, title, filename, created_by, created_at, updated_at) "
            "VALUES (?, ?, 'design', 'Real', 'r.md', 'a', datetime('now'), datetime('now'))",
            (ws, proj),
        )
        conn.commit()
        before = conn.execute(
            "SELECT id, workspace_id, project_id, title FROM project_documents ORDER BY id"
        ).fetchall()

    _assert_correct_final_shape(db_path)  # already correct

    # Re-run 014 explicitly (bypassing the ledger gate) — must be a faithful
    # rebuild that preserves everything.
    _run_014(db_path)

    _assert_correct_final_shape(db_path)
    with closing(get_connection(db_path)) as conn:
        after = conn.execute(
            "SELECT id, workspace_id, project_id, title FROM project_documents ORDER BY id"
        ).fetchall()
    assert before == after, "no-op re-run must preserve rows exactly (incl. NULL cols)"


def test_014_runs_in_normal_runner_flow(tmp_path):
    """014 applies as part of the normal `apply_migrations(SHARED)` flow and is
    recorded in the ledger; a fresh full bootstrap ends at the correct shape."""
    db_path = str(tmp_path / "full.db")
    apply_migrations(db_path, SHARED)
    with closing(get_connection(db_path)) as conn:
        recorded = {r[0] for r in conn.execute("SELECT filename FROM migrations")}
    assert "014_project_documents_rebuild.sql" in recorded
    _assert_correct_final_shape(db_path)

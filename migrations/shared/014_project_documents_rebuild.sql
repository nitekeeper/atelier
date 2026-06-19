-- migrations/shared/014_project_documents_rebuild.sql
-- Forward reconcile: re-assert the intended project_documents shape.
--
-- BACKGROUND (audit, atelier migration-resilience):
-- Migration 005_workspace_less_ops.sql is recorded-applied in some stores
-- but its STRUCTURAL effect is absent: those stores still carry an orphan
-- legacy `type` column on project_documents AND still have NOT NULL on
-- workspace_id / project_id (the pre-005 shape). We do NOT un-record 005
-- (the ledger stays as-is); instead this is a FORWARD migration that
-- performs the 005-style table rebuild IDEMPOTENTLY so every store converges
-- on the correct final shape regardless of which shape it is currently in.
--
-- INTENDED FINAL SHAPE (union of 001 + 002 + 005 + 007):
--   * workspace_id / project_id NULLABLE   (005, §10.4 workspace-less ops)
--   * NO `type` column                     (001 removed it; orphan in some stores)
--   * source_ref TEXT                       (002)
--   * metadata   TEXT                       (007)
--   * FTS5 external-content table project_documents_fts over
--     (title, subdomain, filename)          (002 / re-asserted by 005)
--   * 6 idx_docs_* / source_ref indexes     (001 + 002, re-asserted by 005)
--   * 3 sync triggers (ai / ad / au)        (002, re-asserted by 005)
--
-- SAFE ON BOTH SHAPES. The INSERT column list deliberately names ONLY the
-- columns that survive into the new table — never `type`. A raw .sql file
-- cannot branch on PRAGMA, but selecting only the surviving columns is valid
-- in BOTH cases:
--   * orphan shape  (has `type`, NOT NULL cols): `type` is simply not copied
--     (it is being dropped); every named column exists, so SELECT succeeds.
--   * correct shape (no `type`, nullable cols): the list names exactly the
--     existing columns, so SELECT succeeds and the rebuild is a faithful
--     round-trip (a no-op in effect — same rows, same shape).
-- We never reference a column that might be absent, so no PRAGMA branch is
-- needed.
--
-- ATOMICITY. Wrapped in BEGIN;...COMMIT; — the DROP/RENAME must be all-or-
-- nothing. The runner (scripts/migrate.py) additionally rolls back on any
-- error, so a mid-rebuild failure leaves the store untouched. On a store
-- where the effect already exists this migration still runs cleanly (it just
-- rebuilds an already-correct table into an identically-shaped one), so it is
-- safely repeatable; the migrations-ledger UNIQUE gate prevents re-runs in the
-- normal path, and the runner's reconcile guard records it if the ledger was
-- behind.
--
-- ROW + FTS PRESERVATION. All rows are copied via explicit column list; the
-- FTS index is dropped and rebuilt by backfilling from the new table, exactly
-- as 005 does, so FTS contents are preserved (re-derived from the source rows).
--
-- Foreign keys: workspace_id still REFERENCES workspaces(id) and project_id
-- still REFERENCES projects(id); the FK is enforced only for non-NULL values
-- (SQLite treats NULL as "no reference"), so workspace-less / project-less
-- rows are accepted while real rows stay constrained — identical to 005.
--
-- ASSUMPTION: 014 assumes the 002 (source_ref) and 007 (metadata) columns are
-- present on project_documents — i.e. the documented orphan shape (which still
-- carries source_ref/metadata; only `type` and the NOT NULL constraints are
-- wrong). The INSERT below names source_ref + metadata explicitly, so a store
-- lacking them FAILS LOUD ("no such column") rather than silently producing a
-- wrong shape — which is the correct, intended behavior.

BEGIN;

-- ── Rebuild project_documents with the intended shape ──────────────────────
-- Columns: 001 base (minus the removed `type`) + 002 source_ref + 007 metadata,
-- with workspace_id / project_id NULLABLE per 005 (§10.4).
CREATE TABLE project_documents__rebuild_014 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER REFERENCES workspaces(id),  -- nullable per §10.4 workspace-less ops
    project_id   INTEGER REFERENCES projects(id),    -- nullable per §10.4 (and §6.7 (no-project) keys)
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

-- INSERT names ONLY surviving columns — `type` is intentionally never
-- referenced, so this statement is valid whether or not `type` exists on the
-- old table.
INSERT INTO project_documents__rebuild_014 (
    id, workspace_id, project_id, domain, subdomain, title, filename,
    created_by, index_id, source_ref, metadata, created_at, updated_at
)
SELECT
    id, workspace_id, project_id, domain, subdomain, title, filename,
    created_by, index_id, source_ref, metadata, created_at, updated_at
FROM project_documents;

DROP TABLE project_documents;
ALTER TABLE project_documents__rebuild_014 RENAME TO project_documents;

-- ── Recreate the regular indexes from 001 + 002 ────────────────────────────
CREATE INDEX idx_docs_workspace               ON project_documents(workspace_id);
CREATE INDEX idx_docs_project                 ON project_documents(project_id);
CREATE INDEX idx_docs_domain                  ON project_documents(domain);
CREATE INDEX idx_docs_subdomain               ON project_documents(subdomain);
CREATE INDEX idx_docs_index_id                ON project_documents(index_id);
CREATE INDEX idx_project_documents_source_ref ON project_documents(source_ref);

-- ── Rebuild the FTS5 virtual table + sync triggers from 002 ────────────────
-- The FTS5 external-content table must be dropped + recreated when its host
-- table is rebuilt; the inverted index is stale otherwise. Triggers were
-- dropped along with the host table by DROP TABLE.
DROP TABLE IF EXISTS project_documents_fts;
CREATE VIRTUAL TABLE project_documents_fts USING fts5(
    title,
    subdomain,
    filename,
    content='project_documents',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

INSERT INTO project_documents_fts(rowid, title, subdomain, filename)
SELECT id, title, COALESCE(subdomain, ''), filename FROM project_documents;

CREATE TRIGGER project_documents_ai AFTER INSERT ON project_documents BEGIN
    INSERT INTO project_documents_fts(rowid, title, subdomain, filename)
    VALUES (new.id, new.title, COALESCE(new.subdomain, ''), new.filename);
END;

CREATE TRIGGER project_documents_ad AFTER DELETE ON project_documents BEGIN
    INSERT INTO project_documents_fts(project_documents_fts, rowid, title, subdomain, filename)
    VALUES('delete', old.id, old.title, COALESCE(old.subdomain, ''), old.filename);
END;

CREATE TRIGGER project_documents_au AFTER UPDATE ON project_documents BEGIN
    INSERT INTO project_documents_fts(project_documents_fts, rowid, title, subdomain, filename)
    VALUES('delete', old.id, old.title, COALESCE(old.subdomain, ''), old.filename);
    INSERT INTO project_documents_fts(rowid, title, subdomain, filename)
    VALUES (new.id, new.title, COALESCE(new.subdomain, ''), new.filename);
END;

COMMIT;

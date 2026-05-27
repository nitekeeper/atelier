-- migrations/shared/005_workspace_less_ops.sql
-- Workspace-less operations (atelier#53 / spec §10.4).
--
-- Per spec §10.4, certain writes legitimately have no workspace context:
-- the canonical use case is a workspace-less daily log written before
-- any workspace has been registered (cross-machine "scratch" surface).
-- The §6.7 key format reserves `_no-workspace_/(no-project)/...` for
-- exactly this case.
--
-- Today `project_documents.workspace_id` and `project_documents.project_id`
-- are NOT NULL, which blocks the daily-log surface. SQLite has no
-- in-place "DROP NOT NULL" — the canonical relaxation is the table
-- rebuild pattern:
--
--   1. CREATE TABLE __new with the relaxed constraints
--   2. INSERT INTO __new SELECT ... FROM project_documents
--   3. DROP TABLE project_documents
--   4. ALTER TABLE __new RENAME TO project_documents
--   5. Recreate the FTS5 virtual table + triggers from 002
--   6. Recreate the regular indexes from 001 + 002
--
-- The FTS5 virtual table from migration 002 references project_documents
-- via `content='project_documents'`. SQLite does NOT auto-rebuild this
-- when the host table is dropped + recreated, so we drop and recreate
-- the FTS table explicitly (and re-backfill from the new table). The
-- triggers are dropped automatically when DROP TABLE fires.
--
-- Foreign keys are NOT dropped — workspace_id still REFERENCES
-- workspaces(id) and project_id still REFERENCES projects(id). The FK
-- rule is only enforced when the value is non-NULL (SQLite + most
-- engines treat NULL as "no reference"), so workspace-less / project-
-- less rows are accepted while real rows are still constrained.
--
-- Idempotent via the migrations registry (`migrations.filename` UNIQUE
-- gate in `scripts/migrate.py`).

BEGIN;

-- ── Rebuild project_documents with nullable workspace_id / project_id ──
CREATE TABLE project_documents__new (
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
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Column list mirrors the post-002 schema. A future migration that adds
-- another column to project_documents would need to update this list
-- and either re-run a table-rebuild here or rely on plain ALTER ADD.
INSERT INTO project_documents__new (
    id, workspace_id, project_id, domain, subdomain, title, filename,
    created_by, index_id, source_ref, created_at, updated_at
)
SELECT
    id, workspace_id, project_id, domain, subdomain, title, filename,
    created_by, index_id, source_ref, created_at, updated_at
FROM project_documents;

DROP TABLE project_documents;
ALTER TABLE project_documents__new RENAME TO project_documents;

-- ── Recreate the regular indexes from 001 + 002 ────────────────────────
CREATE INDEX idx_docs_workspace            ON project_documents(workspace_id);
CREATE INDEX idx_docs_project              ON project_documents(project_id);
CREATE INDEX idx_docs_domain               ON project_documents(domain);
CREATE INDEX idx_docs_subdomain            ON project_documents(subdomain);
CREATE INDEX idx_docs_index_id             ON project_documents(index_id);
CREATE INDEX idx_project_documents_source_ref ON project_documents(source_ref);

-- ── Rebuild the FTS5 virtual table + sync triggers from 002 ────────────
-- The FTS5 external-content table needs to be dropped + recreated when
-- its host table is rebuilt; the inverted index is stale otherwise.
-- Triggers were dropped along with the host table by DROP TABLE.
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

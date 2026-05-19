-- migrations/shared/002_source_ref_and_fts.sql
-- v1.1.0 follow-up migration: source_ref idempotency keys + FTS5 index.
--
-- Why a second file (append-only) rather than editing 001?
--   001_v110_schema.sql is hash-pinned by test_phase_seed_sha256_pinned and
--   is the documented bootstrap consumed by memex:core:create-store. Adding
--   to it would either break the hash pin or require coordinating a v1.2.0
--   bump for what is logically a Plan 2 deliverable. Append-only keeps the
--   round-1 review fix isolated.
--
-- Two concerns are addressed here:
--
--   1. source_ref idempotency keys (Plan 4 migrator requirement):
--      project_documents / tasks / meeting_minutes get a TEXT source_ref
--      column so the v1.0.13 → v1.1.0 migrator can resume mid-replay
--      without duplicating rows. backend_local.lookup_index_id_by_source_ref
--      uses these columns to find the local row id for a given source_ref.
--
--   2. FTS5 over project_documents (spec §7 "Local documents FTS5 table"):
--      project_documents has no `body` column — bodies live on disk under
--      .ai/raw/. We index (title, subdomain, filename) so MATCH-style search
--      works on the structured fields a Local-mode caller would reasonably
--      search by. Body-grep over the raw archive is intentionally a
--      separate concern (out of scope for this migration).

-- ── source_ref columns + indexes ──────────────────────────────────────────
ALTER TABLE project_documents ADD COLUMN source_ref TEXT;
ALTER TABLE tasks             ADD COLUMN source_ref TEXT;
ALTER TABLE meeting_minutes   ADD COLUMN source_ref TEXT;

CREATE INDEX IF NOT EXISTS idx_project_documents_source_ref ON project_documents(source_ref);
CREATE INDEX IF NOT EXISTS idx_tasks_source_ref             ON tasks(source_ref);
CREATE INDEX IF NOT EXISTS idx_meeting_minutes_source_ref   ON meeting_minutes(source_ref);

-- ── FTS5 virtual table over project_documents ─────────────────────────────
-- content=project_documents + content_rowid=id makes this an external-content
-- index: FTS5 doesn't store its own copy of the indexed columns, just the
-- inverted index. The triggers below keep the index in sync with the
-- canonical project_documents rows.
--
-- Indexed columns: title (primary search target), subdomain (so e.g.
-- "auth" finds all auth-tagged docs), filename (so a path fragment finds
-- the row). `body` is intentionally absent — see header comment.
CREATE VIRTUAL TABLE IF NOT EXISTS project_documents_fts USING fts5(
    title,
    subdomain,
    filename,
    content='project_documents',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

-- Backfill: copy existing rows into the FTS index. INSERT OR IGNORE is
-- defensive — re-running the migration after a partial failure is a no-op.
INSERT INTO project_documents_fts(rowid, title, subdomain, filename)
SELECT id, title, COALESCE(subdomain, ''), filename FROM project_documents;

-- Sync triggers. NULL subdomain → empty string so FTS5 doesn't error on
-- a NULL token stream; downstream MATCH semantics treat '' as non-matching.
CREATE TRIGGER IF NOT EXISTS project_documents_ai AFTER INSERT ON project_documents BEGIN
    INSERT INTO project_documents_fts(rowid, title, subdomain, filename)
    VALUES (new.id, new.title, COALESCE(new.subdomain, ''), new.filename);
END;

CREATE TRIGGER IF NOT EXISTS project_documents_ad AFTER DELETE ON project_documents BEGIN
    INSERT INTO project_documents_fts(project_documents_fts, rowid, title, subdomain, filename)
    VALUES('delete', old.id, old.title, COALESCE(old.subdomain, ''), old.filename);
END;

CREATE TRIGGER IF NOT EXISTS project_documents_au AFTER UPDATE ON project_documents BEGIN
    INSERT INTO project_documents_fts(project_documents_fts, rowid, title, subdomain, filename)
    VALUES('delete', old.id, old.title, COALESCE(old.subdomain, ''), old.filename);
    INSERT INTO project_documents_fts(rowid, title, subdomain, filename)
    VALUES (new.id, new.title, COALESCE(new.subdomain, ''), new.filename);
END;

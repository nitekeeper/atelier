-- Migration 012: result_journal — content-addressed dispatch-result cache.
--
-- This table backs ResultJournal's optional DB persistence layer.  Each row
-- records one successful agent attempt together with its deterministic key,
-- the envelope, usage accounting, and the digest inputs used to derive the key.
--
-- Design notes
-- ------------
-- * ``key`` is the sha256 hex digest produced by ResultJournal.key() — it is
--   clock-free and RNG-free (constructed solely from content: task_id, persona,
--   phase, model, briefing, upstream_results_digest).
-- * ``briefing_sha`` is sha256(briefing) — the briefing itself can be large;
--   only the digest is stored for auditability.
-- * ``upstream_digest`` is sha256(sorted NUL-joined envelope hashes of all
--   transitive deps) — drives cascade invalidation.
-- * ``envelope_json`` / ``usage_json`` are stored as JSON text (NOT blobs) so
--   they are human-readable in DB tools.
-- * ``attempt`` is recorded for observability, but is NOT part of the key
--   (retries of the same inputs replay the same cached envelope).
-- * ResultJournal.put() is last-write-wins (it assigns the store entry
--   unconditionally).  Under the journal's deterministic keying, the same key
--   always carries the same envelope content, so a re-put is a harmless rewrite
--   of identical data — but the code does NOT enforce a write-once guarantee.

CREATE TABLE IF NOT EXISTS journal_attempts (
    key               TEXT    NOT NULL PRIMARY KEY,
    task_id           TEXT    NOT NULL,
    attempt           INTEGER NOT NULL DEFAULT 1,
    persona           TEXT    NOT NULL DEFAULT '',
    phase             TEXT    NOT NULL DEFAULT '',
    model             TEXT    NOT NULL DEFAULT '',
    briefing_sha      TEXT    NOT NULL DEFAULT '',
    upstream_digest   TEXT    NOT NULL DEFAULT '',
    envelope_json     TEXT    NOT NULL DEFAULT '{}',
    usage_json        TEXT    NOT NULL DEFAULT '{}',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

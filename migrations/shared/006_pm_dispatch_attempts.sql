-- migrations/shared/006_pm_dispatch_attempts.sql
-- PM orchestrator dispatch-state columns + wave-ordering index (atelier#60).
--
-- The wave-5 PM dispatch engine (scripts/dispatch.py wave scheduler) needs
-- durable per-task dispatch state that has nowhere to live in the v1.1.0
-- tasks schema:
--
--   * attempts          -- the §5.2 5-attempt budget per task. Incremented
--                          once per dispatch; a wall-clock soft-kill counts
--                          as an attempt. Counting reply envelopes in
--                          bridge_messages is NOT a substitute: `blocked` and
--                          `needs-input` envelopes also emit replies, so an
--                          envelope count != an attempt count.
--   * last_attempt_at   -- ISO-8601 UTC stamp of the most recent dispatch.
--                          Pairs with the per-attempt 30-min wall-clock cap so
--                          the scheduler can compute stall age without a
--                          side table.
--   * abandon_category  -- the parsed TM-006 abandon-grammar token
--                          (scope|blocked|conflict|capacity|stale_rules|
--                          no_consensus|destructive_rejected|tests_unrecoverable)
--                          surfaced to PM/human on budget exhaustion.
--   * abandoned_ack_at  -- ISO-8601 UTC stamp of PM/human acknowledgement of
--                          an abandoned task. AUDIT ONLY: an abandoned task is
--                          wave-terminal the moment its envelope lands
--                          (TERMINAL_ONLY_STATUSES = {done, abandoned}); the
--                          ack does NOT gate the next wave's dispatch.
--
-- Semantics + style mirror 004 (tasks.parallel_group):
--   * Additive ALTER TABLE ADD COLUMN — idempotent via the migration registry
--     (`migrations.filename` UNIQUE gate in scripts/migrate.py).
--   * NO CHECK constraints — abandon_category is validated in the application
--     layer (scripts/dispatch.py validate_envelope against the abandon
--     grammar), not as a closed SQL enum, matching 004's posture that
--     operator/state data belongs to the app layer. parallel_group stays
--     nullable; nothing here narrows it.
--   * NO PRAGMA user_version bump. user_version is the team-mode BRIDGE
--     schema pin (set to 1 by 003, triple-pinned to SCHEMA_VERSION in
--     dispatch.py / bridge_send.py / bridge_read.py / the rules SKILL
--     frontmatter). These columns are orthogonal to the bridge wire format,
--     so — exactly like 004 and 005, which also changed schema without a
--     bump — user_version is left at 1.
--
-- Index: unlike 004 (which deferred indexing until a consumer existed), the
-- wave-5 scheduler IS that consumer. The wave-ordering query is
--   SELECT ... FROM tasks
--   WHERE project_id = ? AND status NOT IN ('complete','abandoned')
--   ORDER BY parallel_group ASC, created_at ASC, id ASC
-- idx_tasks_wave covers the project_id filter + the full ORDER BY key so the
-- scheduler reads waves index-ordered with no filesort. Column order matches
-- the ORDER BY exactly (project_id leads as the equality-filtered prefix).

ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN last_attempt_at TEXT;
ALTER TABLE tasks ADD COLUMN abandon_category TEXT;
ALTER TABLE tasks ADD COLUMN abandoned_ack_at TEXT;

CREATE INDEX idx_tasks_wave ON tasks(project_id, parallel_group, created_at, id);

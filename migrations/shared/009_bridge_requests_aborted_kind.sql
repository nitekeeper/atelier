-- migrations/shared/009_bridge_requests_aborted_kind.sql
-- Widen bridge_requests.kind CHECK enum: ADD 'aborted' + 'team_delete'
-- (atelier#65 team-mode lifecycle — abort.py + sweep_leaked_teams.py).
--
-- WHY A TABLE REBUILD
-- -------------------
-- SQLite cannot ALTER a CHECK constraint in place — there is no
-- `ALTER TABLE ... DROP/ADD CONSTRAINT` and no `MODIFY COLUMN`. The only
-- supported way to change a column's CHECK is the canonical 12-step
-- table-rebuild dance (https://sqlite.org/lang_altertable.html#otheralter):
-- create a `__new` table with the corrected schema, copy the rows, drop the
-- old table, and RENAME `__new` into place, then recreate the index. We do
-- exactly that here, holding every other column / default / STRICT-ness /
-- the status CHECK byte-identical to 008 so the rebuild is a pure enum widen.
--
-- WHY THESE TWO NEW KINDS NOW
-- ---------------------------
-- atelier has NO team-teardown writer today: nothing transitions
-- teams.status, there is no 'team_delete' bridge kind, and no teardown audit
-- event is written. #65 makes abort.py that writer. It needs two new kinds in
-- the orchestrator<->harness queue:
--   * 'team_delete'  — the durable teardown marker. abort.py enqueues a
--                      'team_delete' row (args_json carries {"team_id": "..."})
--                      so the orchestrator services it into a real harness
--                      TeamDelete call. This is what CLOSES the sweep's
--                      orphan-finder loop: sweep_leaked_teams.py treats a
--                      ready 'create_team' row with NO matching ready
--                      'team_delete' row (and no closed teams.status) as an
--                      orphan. Without a 'team_delete' kind there is nothing
--                      for the orphan-join to subtract, so every create_team
--                      would read as leaked. Symmetric to kaizen's canonical
--                      bridge-DB contract (an enqueued teardown row scoped to
--                      the run), which atelier#65 AC#4 mirrors.
--   * 'aborted'      — the run/cycle abort marker the sweep enqueues (AC#4:
--                      "enqueues an 'aborted' row into the bridge DB scoped to
--                      the current run"), recording that a leaked team's queue
--                      was force-aborted. Distinct from 'team_delete' so the
--                      two intents (mark-aborted vs perform-the-delete) stay
--                      separable in the queue, exactly as the four prior kinds
--                      are method-name-mapped 1:1 by the servicer.
-- Both join the closed CHECK enum (fail-closed storage layer; the servicer /
-- abort.py re-validate too), keeping the kind set string-identical to the
-- intents the orchestrator services by name with zero translation.
--
-- FK SAFETY — bridge_requests has NO inbound or outbound foreign keys:
-- team_pk is a free correlation id (NOT FK'd to teams, per 008), and no other
-- table REFERENCES bridge_requests (verified across migrations/ + scripts/).
-- So the DROP/RENAME cannot orphan or be orphaned by any FK. scripts/migrate.py
-- runs each file via conn.executescript() on a single connection with
-- PRAGMA foreign_keys=ON; the standard rebuild caveat is that you must
-- `PRAGMA foreign_keys=OFF` around a rebuild of a table that PARTICIPATES in
-- FK relationships (so the interim DROP doesn't cascade / fail integrity).
-- Because this table participates in NONE, that toggle is unnecessary AND
-- would be unsafe to attempt here: executescript() implicitly COMMITs before
-- the first statement, but `PRAGMA foreign_keys` is a no-op inside an open
-- transaction — so a mid-script toggle could silently not take effect. We
-- therefore deliberately omit the PRAGMA dance and rely on the no-FK fact.
--
-- user_version — NOT bumped (mirrors 004/005/006/007/008). SCHEMA_VERSION (=1,
-- set by 003) is triple-pinned to SCHEMA_VERSION in dispatch.py / bridge_send.py
-- / bridge_read.py (+ the team-mode-rules SKILL frontmatter) and gates the
-- INTER-AGENT MESSAGE WIRE (bridge_messages, 003). bridge_requests is the
-- orthogonal orchestrator<->Python harness-call seam — widening its kind enum
-- does NOT change the bridge_messages wire format. Bumping user_version here
-- would falsely trip every bridge_send/bridge_read open and hard-fail every
-- team-mode session start.
--
-- STYLE — mirrors 003/008:
--   * STRICT table preserved (SQLite >=3.37). JSON cols stay TEXT.
--   * TEXT ISO-8601 timestamps via strftime('%Y-%m-%dT%H:%M:%fZ','now').
--   * NO connection PRAGMA (journal_mode / synchronous / foreign_keys) here —
--     those are owned by the connection layer (scripts/migrate.py:get_connection).
--   * Append-only follow-up; prior migrations stay hash-pinned and untouched.

------------------------------------------------------------------------
-- 1. New table — IDENTICAL to 008's bridge_requests except the kind CHECK
--    now admits the 6-value set (4 prior DispatchTools method names + the
--    two lifecycle kinds 'aborted' and 'team_delete'). Every other column,
--    default, NOT NULL, the status CHECK, and STRICT are verbatim from 008.
------------------------------------------------------------------------
CREATE TABLE bridge_requests__new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT, -- AUTOINCREMENT guarantees id > 0 + monotonic FIFO ordering
    team_pk       TEXT NOT NULL,                     -- run/cycle correlation id; scopes the servicer's pending scan
    kind          TEXT NOT NULL
                    CHECK(kind IN ('create_team','spawn_teammate','send_message','spawn_subagent','aborted','team_delete')),
    args_json     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','ready','error')),
    response_json TEXT,
    error_text    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at  TEXT
) STRICT;

-- 2. Copy every existing row verbatim. Column order in bridge_requests__new
--    matches 008 exactly, so `SELECT *` aligns positionally 1:1.
INSERT INTO bridge_requests__new SELECT * FROM bridge_requests;

-- 2b. PRESERVE the AUTOINCREMENT high-water mark. A plain `INSERT ... SELECT *`
--     re-seeds the rebuilt table's sqlite_sequence from MAX(id) of the COPIED
--     rows — NOT from the original sequence. If id N was previously allocated
--     then its row deleted, the old sequence is N but MAX(id) < N, so the next
--     INSERT would REUSE id N — breaking the header/§82 "monotonic FIFO, never
--     reused" invariant. SQLite's canonical rebuild guidance calls out
--     preserving sqlite_sequence for AUTOINCREMENT tables, so we carry the old
--     high-water mark forward. We take MAX of the pre-existing sequence (under
--     the OLD table's name, which has not been dropped yet) and the new table's
--     own seed (post-copy) so the mark only ever moves UP, never down.
--
--     NOTE: this runs BEFORE the DROP/RENAME below — at this point both
--     `bridge_requests` (old) and `bridge_requests__new` have rows in
--     sqlite_sequence, so MAX() over the two names is well-defined.
UPDATE sqlite_sequence
   SET seq = (
       SELECT MAX(seq) FROM sqlite_sequence
        WHERE name IN ('bridge_requests', 'bridge_requests__new')
   )
 WHERE name = 'bridge_requests__new';

-- 3. Drop the old table and rename the rebuilt one into its place. Dropping
--    `bridge_requests` removes its own sqlite_sequence row; the RENAME below
--    carries the `bridge_requests__new` sequence row (now holding the preserved
--    high-water mark) forward under the new table name.
DROP TABLE bridge_requests;
ALTER TABLE bridge_requests__new RENAME TO bridge_requests;

-- 4. Recreate the servicer's covering index — RENAME does NOT carry indexes
--    from the __new table name forward, so it is rebuilt verbatim from 008.
CREATE INDEX idx_bridge_requests_team_pending
    ON bridge_requests(team_pk, status, id);

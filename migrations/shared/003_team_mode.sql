-- migrations/shared/003_team_mode.sql
-- Atelier team-mode foundationals (epic #37; design
-- docs/specs/2026-05-25-atelier-team-mode-design.md, commit de1de0b04f).
--
-- Adds the substrate for multi-agent "team" sessions: team registry,
-- per-member roster (with immutable persona snapshots), append-only
-- bridge log, mutable per-recipient delivery cursors, shutdown handshake
-- records, and a team-level audit trail.
--
-- Style mirrors 001/002:
--   * TEXT ISO-8601 timestamps via strftime('%Y-%m-%dT%H:%M:%fZ','now').
--   * No PRAGMA journal_mode / synchronous / busy_timeout / foreign_keys
--     here — those are connection-scoped and applied by the connection
--     layer (scripts/migrate.py.get_connection / scripts/backend_local._conn).
--   * Append-only follow-up; 001 stays hash-pinned and untouched.
--
-- All tables are STRICT (SQLite ≥3.37). Strict-mode column types are
-- limited to INT/INTEGER/REAL/TEXT/BLOB/ANY; defaults, CHECK constraints,
-- AUTOINCREMENT, generated columns (STORED) are compatible.
--
-- Cross-cutting decisions captured in the Phase 3 mesh close:
--   * Idempotency UNIQUE scope = per-team (NOT per-recipient). A fan-out
--     send shares one key across recipients; uniqueness belongs to the
--     send call, not the row. Per-recipient FIFO is still guaranteed by
--     the separate (team_id, recipient, seq) uniqueness.
--   * Delivery cursors live in bridge_delivery — NOT as a writable column
--     on bridge_messages — so the append-only triggers on the log
--     remain absolute.
--   * persona_snapshots is immutable (no UPDATE/DELETE). team_members
--     and bridge_messages reference it by id so renamed-but-mutated
--     personas cannot retro-rewrite history (ai-safety-1 + ai-ethicist-1).
--   * Retention / archival / v_bridge_lag view deferred from v1.

------------------------------------------------------------------------
-- teams -- one row per active multi-agent team session
------------------------------------------------------------------------
CREATE TABLE teams (
    team_id        TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    lead_role      TEXT NOT NULL,
    status         TEXT NOT NULL CHECK(status IN ('active','shutting_down','closed')),
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

------------------------------------------------------------------------
-- persona_snapshots -- immutable captures of an Atelier persona at the
-- moment a teammate is spawned. Pinning every team_members row + every
-- bridge_messages row to a snapshot id prevents a later persona edit
-- from retro-rewriting "who said what under which persona".
--
-- Enforced immutability via trg_persona_no_update / trg_persona_no_delete
-- below. Created before team_members because team_members carries a NOT
-- NULL FK to it.
------------------------------------------------------------------------
CREATE TABLE persona_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_version TEXT NOT NULL,
    persona_blob    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

------------------------------------------------------------------------
-- team_members -- roster of teammates within a team. PK (team_id, role_id)
-- means a role joins a team at most once; dispatch.py uses this for the
-- idempotent spawn. persona_snapshot_id pins the persona at spawn time.
------------------------------------------------------------------------
CREATE TABLE team_members (
    team_id             TEXT NOT NULL REFERENCES teams(team_id),
    role_id             TEXT NOT NULL,
    member_name         TEXT NOT NULL,
    wave                INTEGER NOT NULL DEFAULT 0,
    last_heartbeat      TEXT,
    persona_snapshot_id INTEGER NOT NULL REFERENCES persona_snapshots(id),
    joined_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (team_id, role_id)
) STRICT;

------------------------------------------------------------------------
-- bridge_messages -- append-only inter-agent message log.
--
--   * seq is per-(team_id, recipient); allocated by bridge_send.py under
--     BEGIN IMMEDIATE as MAX(seq)+1. ux_bridge_pkey enforces uniqueness;
--     the test_bridge_concurrency invariants (no-gap, no-dup, FIFO) ride
--     on top.
--   * idempotency_key is OPTIONAL and TEAM-scoped (ux_bridge_idem below).
--     Replay returns the prior seq AND prior persona_snapshot_id — never
--     re-stamps under a newer persona.
--   * payload length is hard-capped at 8 KiB (prompt-engineer-1's 8k
--     writer ceiling); the CHECK keeps oversized messages out of the log.
--   * byte_len is STORED-generated for cheap lag/observability queries.
--   * causal_ref (cog-sci-1): nullable seq this message replies to, so
--     reviewers and late-joiners can rebuild adjacency pairs without LLM
--     inference. Soft reference — no FK because (team_id, recipient,
--     causal_ref) might not exist in a malformed replay scenario.
--   * Composite sender FK to team_members(team_id, role_id) blocks at
--     the DB layer any attempt to forge a sender that hasn't been
--     dispatched into this team.
--
-- Append-only enforced by trg_bridge_no_update / trg_bridge_no_delete.
------------------------------------------------------------------------
CREATE TABLE bridge_messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT, -- AUTOINCREMENT guarantees id > 0; no explicit CHECK needed
    team_id             TEXT NOT NULL REFERENCES teams(team_id),
    recipient           TEXT NOT NULL,
    seq                 INTEGER NOT NULL,
    sender_id           TEXT NOT NULL,
    idempotency_key     TEXT,
    causal_ref          INTEGER,
    kind                TEXT NOT NULL
                          CHECK(kind IN ('spawn','reply','shutdown_req','shutdown_resp','heartbeat')),
    wave                INTEGER,
    -- Payload byte cap: CAST(... AS BLOB) makes length() count UTF-8 bytes,
    -- matching scripts/bridge_send.py's len(payload.encode('utf-8')) gate.
    -- Both gates must agree or multi-byte payloads diverge between writer
    -- and direct INSERT (test seeding). Do NOT remove the CAST without
    -- updating bridge_send.py and adding a migration row.
    payload             TEXT NOT NULL CHECK(length(CAST(payload AS BLOB)) <= 8192),
    byte_len            INTEGER GENERATED ALWAYS AS (length(CAST(payload AS BLOB))) STORED, -- bytes, matches the payload CHECK + bridge_send's UTF-8 enforcement
    persona_snapshot_id INTEGER NOT NULL REFERENCES persona_snapshots(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (team_id, sender_id) REFERENCES team_members(team_id, role_id)
) STRICT;

-- Per-team idempotency scope (Phase 3 mesh close, final).
-- Partial unique: only enforced on rows that actually carry a key.
CREATE UNIQUE INDEX ux_bridge_idem
    ON bridge_messages(team_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Per-recipient FIFO uniqueness — the seq allocator's invariant.
CREATE UNIQUE INDEX ux_bridge_pkey
    ON bridge_messages(team_id, recipient, seq);

-- Global tailers / audit scans across all recipients of a team.
CREATE INDEX ix_bridge_team_seq
    ON bridge_messages(team_id, seq);

------------------------------------------------------------------------
-- bridge_delivery -- mutable per-recipient delivery cursor.
--
-- Lives in its own table because bridge_messages is append-only. Readers
-- UPDATE this row to advance the cursor; the log itself is never touched.
-- (team_id, recipient) PK matches the natural read scope.
------------------------------------------------------------------------
CREATE TABLE bridge_delivery (
    team_id      TEXT NOT NULL,
    recipient    TEXT NOT NULL,
    last_seq     INTEGER NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (team_id, recipient)
) STRICT;

------------------------------------------------------------------------
-- shutdown_requests -- handshake records for team / member shutdown.
-- request_id is a TEXT (caller-supplied UUID/ULID) so the response can
-- echo it back per the protocol contract in team-mode-rules SKILL.md.
------------------------------------------------------------------------
CREATE TABLE shutdown_requests (
    request_id   TEXT PRIMARY KEY,
    team_id      TEXT NOT NULL REFERENCES teams(team_id),
    requested_by TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

------------------------------------------------------------------------
-- team_audit_log -- append-by-convention trail of team-level events
-- (dispatch, wave-advance, shutdown, abandonment). Not enforced
-- append-only at the schema layer — distinct from the bridge log; this
-- is operational history, not the canonical inter-agent message stream.
------------------------------------------------------------------------
CREATE TABLE team_audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id    TEXT NOT NULL REFERENCES teams(team_id),
    event_type TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

------------------------------------------------------------------------
-- Auxiliary indexes (hot-path reads beyond the bridge log).
------------------------------------------------------------------------
CREATE INDEX ix_members_heartbeat
    ON team_members(team_id, last_heartbeat);

CREATE INDEX ix_shutdown
    ON shutdown_requests(team_id, status, created_at);

------------------------------------------------------------------------
-- Append-only / immutability triggers.
--
-- bridge_messages: the canonical message log MUST be append-only or the
-- provenance & idempotency replay guarantees collapse. Readers advance
-- bridge_delivery instead of UPDATE-ing the log row.
--
-- persona_snapshots: a snapshot is an immutable capture; mutating it
-- would silently rewrite the persona under which prior messages were
-- sent. DELETE forbidden so historical bridge_messages can always
-- resolve their persona_snapshot_id.
------------------------------------------------------------------------
CREATE TRIGGER trg_bridge_no_update
    BEFORE UPDATE ON bridge_messages
    BEGIN
        SELECT RAISE(ABORT, 'bridge_messages is append-only');
    END;

CREATE TRIGGER trg_bridge_no_delete
    BEFORE DELETE ON bridge_messages
    BEGIN
        SELECT RAISE(ABORT, 'bridge_messages is append-only');
    END;

CREATE TRIGGER trg_persona_no_update
    BEFORE UPDATE ON persona_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'persona_snapshots is immutable');
    END;

CREATE TRIGGER trg_persona_no_delete
    BEFORE DELETE ON persona_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'persona_snapshots is immutable');
    END;

------------------------------------------------------------------------
-- SCHEMA_VERSION pin. Runtime asserts (bridge_send / bridge_read /
-- dispatch) compare PRAGMA user_version against the in-code constant
-- SCHEMA_VERSION (=1) on every open; mismatch = hard fail.
------------------------------------------------------------------------
PRAGMA user_version = 1;

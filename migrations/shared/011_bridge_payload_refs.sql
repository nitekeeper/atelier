-- migrations/shared/011_bridge_payload_refs.sql
-- Out-of-band payload referencing for the inter-agent bridge (cycle 1 —
-- payload referencing; Bedrock payload-referencing / memory-pointer prior art).
--
-- Problem: bridge_messages.payload is hard-capped at 8 KiB
-- (003_team_mode.sql CHECK length(CAST(payload AS BLOB)) <= 8192, mirrored
-- by bridge_send.py's PAYLOAD_MAX_BYTES). Today an oversized inter-agent
-- payload is HARD-REJECTED at send. This migration adds a content-addressed
-- side-store so large bodies persist out-of-band; bridge_send substitutes a
-- short reference tag into the (still <=8 KiB) message, and bridge_read
-- dereferences on demand. This keeps the 8 KiB wire CHECK intact while
-- letting the actual body exceed it, and keeps orchestrator/teammate context
-- carrying TAGS not bodies (targets the F15 read-first context-bloat aborts).
--
-- Style mirrors 003 (team-mode foundationals) and 010 (additive follow-up):
--   * STRICT table; TEXT ISO-8601 created_at via strftime.
--   * Byte-counting idiom byte_len = length(CAST(body AS BLOB)) is IDENTICAL
--     to 003's payload/byte_len gate so the writer's
--     len(body.encode('utf-8')) and the stored byte_len never diverge on
--     multi-byte (non-ASCII) bodies. The content-address sha256 is computed
--     by scripts/bridge_payloads.py over the SAME UTF-8 byte sequence.
--   * Content-addressed PRIMARY KEY (team_id, sha256): identical bodies
--     within a team collapse to ONE row, so the store write is naturally
--     idempotent (INSERT OR IGNORE) — this is what makes bridge_send's
--     idempotency-replay safe (a replay racing a first-send dereferences an
--     already-present row, never a missing pointer).
--   * NO length CHECK on body: the whole point is to hold payloads that
--     exceed the 8 KiB wire cap. An application-layer 1 MiB sanity ceiling
--     lives in scripts/bridge_payloads.py (NOT a schema CHECK — keeping the
--     ceiling tunable without a migration).
--   * Composite team scope FK to teams(team_id): pointer rows are reclaimable
--     by a future team-teardown sweep. Auto-GC is DEFERRED (no trigger/TTL
--     today) but the scoping column is present by design so the reclamation
--     path is never designed out.
--   * Append-only via trg_payloads_no_update / trg_payloads_no_delete,
--     mirroring 003's bridge_messages discipline: a content-addressed store
--     MUST NOT mutate a body under a live sha256 (that would make the hash
--     lie) nor delete one a live message still references. DELETE is reserved
--     for the deferred sweep, which will land as its own migration that drops
--     trg_payloads_no_delete under a documented teardown contract.
--   * NO user_version bump. The bridge wire pin stays 1 — bridge_send.py and
--     bridge_read.py BOTH hard-fail when PRAGMA user_version != SCHEMA_VERSION
--     (=1) on every open. This migration is additive (new table + new
--     nullable column), orthogonal to the wire protocol version, so it leaves
--     user_version at 1 (matches 004/005/006/007/010's 'left at 1' posture).
--     DO NOT add `PRAGMA user_version = ...` here.

------------------------------------------------------------------------
-- bridge_payloads -- content-addressed, append-only out-of-band store
-- for oversized inter-agent payload bodies.
--
--   * (team_id, sha256) PK = content address scoped per team. Dedup +
--     idempotent INSERT OR IGNORE ride on this PK.
--   * byte_len mirrors 003's byte-counting idiom EXACTLY so writer-side
--     len(body.encode('utf-8')) and on-disk byte_len agree for multi-byte
--     bodies. Stored (not generated) because the writer supplies it
--     alongside the sha256 it already computed — both come from one
--     body.encode('utf-8') pass in scripts/bridge_payloads.py; a CHECK
--     pins them to the canonical byte count so a wrong writer value is
--     rejected rather than silently stored.
--   * body has NO length cap (the reason this store exists).
------------------------------------------------------------------------
CREATE TABLE bridge_payloads (
    team_id    TEXT NOT NULL REFERENCES teams(team_id),
    sha256     TEXT NOT NULL,
    -- byte_len must equal the UTF-8 byte length of body. Pinning it with a
    -- CHECK (not a GENERATED column) lets the writer pass the value it
    -- already computed while still rejecting any divergence — the canonical
    -- count is length(CAST(body AS BLOB)), identical to 003's gate.
    byte_len   INTEGER NOT NULL CHECK(byte_len = length(CAST(body AS BLOB))),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (team_id, sha256)
) STRICT;

------------------------------------------------------------------------
-- bridge_messages.payload_ref -- out-of-band reference indicator.
--
-- Nullable additive column (SQLite cannot ADD a NOT NULL column without a
-- constant default; NULL is the correct backfill = 'inline payload, no
-- out-of-band body'). When non-NULL it carries the reference tag that
-- bridge_send substituted; bridge_read keys off its presence to decide
-- whether --resolve has a body to dereference. Making the reference a
-- first-class column (D3 unforgeability) means a referenced message is
-- distinguishable from one whose inline payload merely happens to contain
-- ref-tag-looking text. NO FK: payload_ref holds the sha256 directly; the
-- application layer pairs it with the message row's OWN team_id column to hit
-- bridge_payloads (team_id, sha256) — the team scope is never parsed out of
-- payload_ref itself.
------------------------------------------------------------------------
ALTER TABLE bridge_messages ADD COLUMN payload_ref TEXT;

------------------------------------------------------------------------
-- Append-only / immutability triggers — mirror 003's bridge_messages and
-- persona_snapshots discipline. A content-addressed body MUST NOT change
-- under its hash, and MUST NOT vanish while a live message references it.
------------------------------------------------------------------------
CREATE TRIGGER trg_payloads_no_update
    BEFORE UPDATE ON bridge_payloads
    BEGIN
        SELECT RAISE(ABORT, 'bridge_payloads is append-only');
    END;

CREATE TRIGGER trg_payloads_no_delete
    BEFORE DELETE ON bridge_payloads
    BEGIN
        SELECT RAISE(ABORT, 'bridge_payloads is append-only');
    END;

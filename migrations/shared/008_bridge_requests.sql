-- migrations/shared/008_bridge_requests.sql
-- Production dispatch binding — orchestrator<->Python harness-call queue
-- (atelier#81). The live queue-bridge transport for the already-merged
-- dispatch SEAM (#60 WaveDispatcher, #61 build_spawn_fn/DispatchTools,
-- #62 resolve_dispatch_mode). Mirrors kaizen's proven
-- scripts/bridge_db.py + cc_tool_bridge.py QueueBridgeWrapper pattern.
--
-- WHAT THIS TABLE IS
-- ------------------
-- scripts/dispatch.py is pure Python and CANNOT call the Claude Code harness
-- tools (Agent / TeamCreate / SendMessage) directly — those exist only inside
-- an active orchestrator turn-loop. bridge_requests is the seam: a Python
-- DispatchTools wrapper ENQUEUES a row (status='pending'); the orchestrator
-- services pending rows per-turn (internal/bridge-poll/SKILL.md), performs the
-- real tool call, and writes back response_json + flips status to 'ready' (or
-- 'error'). The blocking create_team poller reads its own row back; the three
-- fire-and-forget kinds (spawn_teammate / send_message / spawn_subagent) never
-- poll. This is the orchestrator<->harness call seam — DISTINCT from the
-- INTER-AGENT message wire in bridge_messages (003).
--
-- kind ENUM — string-identical to the DispatchTools Protocol method names in
-- scripts/dispatch.py (create_team / spawn_teammate / send_message /
-- spawn_subagent) so the orchestrator servicer maps kind->method by name with
-- ZERO translation. A closed CHECK enum rejects an out-of-set / injected kind
-- at the storage layer (fail-closed; the servicer re-validates too).
--
-- status ENUM — the 3-state ('pending','ready','error'). 'error' lets a
-- serviced-but-FAILED harness call be represented so the blocking create_team
-- poller raises instead of spinning forever (mirrors kaizen's bridge_db status
-- CHECK). Idempotency hinges on this column: only 'pending' rows are picked up
-- by the servicer, so a flip is the "claimed" key — a retry never double-spawns.
--
-- ARGS / RESPONSE — args_json carries the tool-call args (vocabulary-identical
-- to kaizen's bridge_db: args_json, NOT request_json); response_json carries the
-- servicer's reply (e.g. {"team_id": "..."} for create_team); error_text carries
-- a serviced-but-failed diagnostic. All three are TEXT (STRICT tables forbid a
-- JSON column type; JSON is serialized to TEXT, exactly like 003's payloads).
--
-- INDEX — idx_bridge_requests_team_pending(team_pk, status, id) covers the
-- servicer's hot query ("pending rows for this cycle, FIFO by id") and is
-- *exactly* index-ordered when the servicer uses `ORDER BY id`: team_pk +
-- status are equality predicates, id is the leading sort key in the residual
-- index suffix, so SQLite reads the index in order with no sort step. FIFO is
-- carried by `id` alone (AUTOINCREMENT-monotonic), which is what
-- internal/bridge-poll/SKILL.md's servicer ORDER BY uses. `created_at` is NOT
-- in the index — an `ORDER BY created_at, id` would force a sort, so it is not
-- used; insertion order and `id` order coincide. We key on a generic run/team
-- correlation id (team_pk) so the servicer can scope to one cycle's queue.
--
-- STYLE — mirrors 003_team_mode.sql exactly:
--   * STRICT table (SQLite >=3.37). JSON cols are TEXT (no JSON type in STRICT).
--   * TEXT ISO-8601 timestamps via strftime('%Y-%m-%dT%H:%M:%fZ','now').
--   * NO PRAGMA journal_mode / synchronous / busy_timeout / foreign_keys here —
--     those are connection-scoped and applied by the connection layer
--     (scripts/migrate.py:get_connection / scripts/backend_local._conn, and the
--     wrapper's own WAL+busy_timeout open in scripts/dispatch.py).
--   * Append-only follow-up; prior migrations stay hash-pinned and untouched.
--
-- user_version — NOT bumped. SCHEMA_VERSION (=1, set by 003) is triple-pinned
-- in dispatch.py / bridge_send.py / bridge_read.py (+ the team-mode-rules SKILL
-- frontmatter) and gates the INTER-AGENT MESSAGE WIRE (bridge_messages). This
-- table is the orchestrator<->Python harness-call seam — orthogonal to that
-- wire format. 006 set the precedent: additive non-wire tables/columns leave
-- user_version at 1 (cf. 004 / 005 / 006, which all changed schema without a
-- bump). Bumping here would falsely trip every bridge_send/bridge_read open.

------------------------------------------------------------------------
-- bridge_requests -- orchestrator<->Python harness-call queue.
--
--   * kind CHECK enum == the four DispatchTools method names (name-mapped).
--   * status CHECK enum == the 3-state ('pending','ready','error').
--   * args_json / response_json / error_text are TEXT (STRICT-mode JSON).
--   * Only 'pending' rows are serviced; a status flip is the idempotency key.
------------------------------------------------------------------------
CREATE TABLE bridge_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT, -- AUTOINCREMENT guarantees id > 0 + monotonic FIFO ordering
    team_pk       TEXT NOT NULL,                     -- run/cycle correlation id; scopes the servicer's pending scan
    kind          TEXT NOT NULL
                    CHECK(kind IN ('create_team','spawn_teammate','send_message','spawn_subagent')),
    args_json     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','ready','error')),
    response_json TEXT,
    error_text    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at  TEXT
) STRICT;

-- Covering index for the servicer's hot query: pending rows for one cycle's
-- queue, FIFO by id. Mirrors kaizen's idx_bridge_requests_run_pending shape.
CREATE INDEX idx_bridge_requests_team_pending
    ON bridge_requests(team_pk, status, id);

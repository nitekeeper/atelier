-- Migration 013: drop bridge_requests — M7 PR-B dispatch-queue removal.
--
-- M7 PR-B removed the bridge DISPATCH QUEUE: the queue dispatcher plus the
-- abort/sweep/teardown lifecycle were deleted, so `bridge_requests` (created in
-- 008, rebuilt in 009) now has NO remaining producer or consumer. Nothing FKs
-- to it. The inter-agent message WIRE (`bridge_messages` / `bridge_payloads`)
-- is a separate seam and is UNAFFECTED by this drop.
--
-- Forward-only; NOT reversible. `IF EXISTS` on both the index and the table so
-- replay and partial-state DBs never raise (the migrate runner records this
-- filename and skips it on subsequent bootstraps, but the guards make a raw
-- re-run safe too).
--
-- user_version — NOT bumped (consistent with 008/009 and every prior migration;
-- migrations are tracked purely by filename in the `migrations` table).

DROP INDEX IF EXISTS idx_bridge_requests_team_pending;
DROP TABLE IF EXISTS bridge_requests;

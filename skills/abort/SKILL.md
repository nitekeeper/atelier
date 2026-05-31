---
description: Use when tearing down a live agent-team — a graceful (soft) or forced (--hard) abort that records a durable abort-report, sets the team status, and enqueues the TeamDelete the live session then services.
---

# abort

Team-mode lifecycle teardown. Records a durable abort-report, transitions `teams.status`, and enqueues a `team_delete` bridge row for the current team. Python cannot call the session-scoped `TeamDelete` harness tool, so the LIVE orchestrator session finishes the teardown on its bridge-poll loop after this script returns.

## When to use

Call `abort` to deliberately tear down the CURRENT agent-team — the user wants to stop the cycle, or the team has stalled past recovery. (For LEAKED teams from a prior, dead session, use `scripts/sweep_leaked_teams.py` instead; this skill is the in-session teardown.)

## Pre-flight (always first)

Run `from scripts.atelier_entrypoint import startup_check; startup_check()`.

Branch on the returned `action`:

- **`proceed-local`** — Memex is not installed. Continue with the rest of
  this skill's recipe; all writes go to the project-local `.ai/atelier.db`.
- **`proceed-memex`** — Memex is installed and bootstrapped. Continue;
  all writes go through Memex.
- **`prompt-migration`** — Memex is installed but this project still
  has a local DB. Read `internal/migrate-local-to-memex/SKILL.md` and
  follow its prompt protocol. After the user answers, restart the
  pre-flight (`startup_check()` will now return `proceed-memex` or
  `proceed-local` depending on the user's choice).

## Procedure

### 1. Identify the team_pk

The `--team-pk` is the run/cycle correlation id that scopes the team's bridge queue — the same value passed to `create_team` when the cycle started. The team_id is resolved for you from that cycle's `create_team` bridge row (its `response_json.$.team_id`); pass `--team-id` explicitly only if you already hold it or resolution would fail.

### 2. Run the abort script

From the target project root:

```
PYTHONPATH=. python3 -m scripts.abort --team-pk <pk> [--hard] [--reason "<why>"]
```

- **soft (default)** — graceful teardown: `teams.status -> 'shutting_down'`.
- **`--hard`** — forced teardown: report written FIRST, then `teams.status -> 'closed'`.
- `--reason "<why>"` — recorded in the report + the `aborted` audit event (defaults to `operator-initiated abort`).
- `--db` / `--bridge-db` both default to `.ai/atelier.db` (atelier's single project-local DB); pass nothing unless overriding.
- `--clean-worktree` — soft path only; remove the worktree iff it is clean (see step 4).

The script is mode-aware: in **Local mode** it performs the full teardown; in **non-local** mode the state mutators raise `NotImplementedError`, so it WARNs, skips the DB mutations, still writes the report where possible, and returns 0.

### 3. What the script does — and what it CANNOT do

In Local mode `abort.py` performs three durable writes (the shared core, run by BOTH paths):

1. **Abort-report** — `backend.write_document(domain='postmortem', subdomain='abort', ...)`, a durable markdown postmortem. On `--hard` this is written FIRST so the report survives even if a later step fails.
2. **`teams.status`** — set to `shutting_down` (soft) or `closed` (hard).
3. **One `team_delete` bridge row** — `kind='team_delete'`, `status='pending'`, scoped to `team_pk`, carrying `args_json={"team_id": ...}`. This both INSTRUCTS the live session to reap the team AND records the teardown — the symmetric subtractor that closes the sweep's orphan-join. A `team_audit_log` `aborted` event is written alongside.

**What the script CANNOT do:** Python cannot call the session-scoped `TeamDelete` harness tool. The script only ENQUEUES the `team_delete` row. The LIVE orchestrator session must finish the teardown ON ITS NEXT TURN:

- **soft** — drive the TM-005 `shutdown_req` / `shutdown_resp` graceful handshake on the `bridge_messages` wire first (each teammate echoes the `request_id` within one turn; see `internal/team-mode-rules/SKILL.md` TM-005). Only AFTER every teammate has acknowledged (or its grace window elapsed) do you service the `team_delete` row by calling `TeamDelete` for that `team_id`.
- **hard** — skip the handshake. Service the `team_delete` row IMMEDIATELY: call `TeamDelete` per `team_id` on the next turn.

**Servicing is HANDLED HERE, not by the WaveDispatcher's `bridge-poll` servicer.** The `team_delete` / `aborted` lifecycle kinds are NOT in `internal/bridge-poll/SKILL.md`'s closed-enum switch (which services only the four `create_team` / `spawn_teammate` / `send_message` / `spawn_subagent` dispatch kinds and treats anything else as out-of-enum). This abort SKILL is the authority for the lifecycle kinds: after you call `TeamDelete` for a `team_delete` row, mark it serviced yourself so it is not re-picked-up:

```sql
UPDATE bridge_requests
SET status = 'ready',
    completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE id = :team_delete_row_id;
```

Flipping the row to `status='ready'` is also what lets `scripts/sweep_leaked_teams.py`'s orphan-join subtract the team (filter (i) matches `team_delete` rows with `status='ready'`). Until you flip it, a soft-aborted team (whose `teams.status` is `shutting_down`, not `closed`) may be re-reported by a later sweep — which is SAFE because TeamDelete is idempotent, but flipping the row promptly closes the window. Any cross-session team config directory left on disk by a crashed prior run is filesystem-only cleanup: `rm -rf ~/.claude/teams/<team_id>/`.

### 4. Worktree preservation

The script NEVER destroys uncommitted work. If the current worktree is dirty (or its state cannot be read), it is ALWAYS preserved — on `--hard` it is preserved with a warning. A CLEAN worktree is auto-removed only on `--hard`, or on the soft path only when you pass `--clean-worktree`. The actual worktree decision is folded into the report's "what was torn down" section.

## Hard rules

- Never assume `abort.py` deleted the team — it only enqueued the request. The LIVE session MUST service the `team_delete` row, or the team leaks.
- soft path: complete the TM-005 handshake BEFORE servicing `team_delete`; never reap teammates out-of-band on the graceful path.
- Never destroy a dirty worktree to force a clean teardown — preservation is intentional.
- `--db` defaults to `.ai/atelier.db`; pass no db path unless overriding the target.

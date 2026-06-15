---
description: Use when tearing down a live agent-team — a graceful (soft) or forced (--hard) abort that records a durable abort-report postmortem, writes the 'aborted' audit event (the resume signal), sets the team status, and applies the worktree policy.
---

# abort

Team-mode lifecycle teardown. Under the host engine the workers are reaped by the
engine itself — there is no harness team to `TeamDelete`. Abort is therefore a
**recorder, not a reaper**: it writes a durable abort-report postmortem, an
`'aborted'` `team_audit_log` event (the authoritative resume signal), transitions
`teams.status`, and applies the worktree policy.

## When to use

Call `abort` to deliberately tear down the CURRENT agent-team cycle — the user
wants to stop the cycle, or the team has stalled past recovery. This skill is the
in-session teardown recorder.

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

### 1. Identify the team_pk and team_id

The `--team-pk` is the run/cycle correlation id that scopes the cycle's records —
the same value used when the cycle started. The `--team-id` is the team's TEXT id;
it is **required for the `'aborted'` audit event** (`team_audit_log.team_id`
references `teams(team_id)`). Pass `--team-id` whenever you hold it — without it
the audit event is SKIPPED and resume detection cannot find the arc (the
abort-report is still written).

### 2. Run the abort script

From the target project root:

```
PYTHONPATH=. python3 -m scripts.abort --team-pk <pk> [--team-id <id>] --project-id <teams.project_id> --phase <projects.phase> [--hard] [--reason "<why>"]
```

- **soft (default)** — graceful teardown: `teams.status -> 'shutting_down'`.
- **`--hard`** — forced teardown: report written FIRST, then `teams.status -> 'closed'`.
- `--reason "<why>"` — recorded in the report + the `aborted` audit event (defaults to `operator-initiated abort`).
- `--db` / `--bridge-db` both default to `.ai/atelier.db` (atelier's single project-local DB); pass nothing unless overriding.
- `--clean-worktree` — soft path only; remove the worktree iff it is clean (see step 4).
- **`--project-id <teams.project_id>`** and **`--phase <projects.phase>`** — the #66 resume hooks. The orchestrator already holds the cycle's textual `teams.project_id` correlation string and the live `projects.phase` (the phase the arc is being aborted AT), so pass them BOTH. They are folded into the `aborted` audit payload (the authoritative resume signal) and the abort-report metadata so the NEXT `/atelier:run` pre-flight (`scripts.resume.find_resumable_arc`) can OFFER to continue FROM this abort point (AC3) and force-phase AT it (AC4). **Omitting them is silently degenerate:** `abort_phase`/`project_id` default to `None`, so the resume prompt renders "aborted arc was found at phase `None`" and there is no phase to continue at — the offer still fires (never-silent holds) but its CONTENT is unusable. Always thread both on a live abort.

The script is mode-aware: in **Local mode** it performs the full teardown; in **non-local** mode the state mutators are skipped (team-state mutation is Local-only), so it WARNs, writes the report where possible, and returns 0.

### 3. What the script does

In Local mode `abort.py` performs the durable writes (the shared core, run by BOTH paths):

1. **Abort-report** — `backend.write_document(domain='postmortem', subdomain='abort', ...)`, a durable markdown postmortem. On `--hard` this is written FIRST so the report survives even if a later step fails. (The workspace-less Memex write now persists in non-local mode too — #90 part-3 — so the report holds cross-mode.)
2. **`teams.status`** — set to `shutting_down` (soft) or `closed` (hard).
3. **`'aborted'` `team_audit_log` event** — `backend.write_team_audit(event_type='aborted', ...)`, carrying the #66 resume hooks (`project_id`, `abort_phase`, `incomplete_task_ids`) threaded from `--project-id` / `--phase` (step 2). This payload is the AUTHORITATIVE resume signal `scripts.resume.find_resumable_arc` reads on the next pre-flight (it joins `team_audit_log.team_id -> teams.project_id`, since the workspace-less abort doc carries `project_id=None` at the column level). The event is SKIPPED when `--team-id` is unresolved (the FK to `teams` needs it); the report still records the abort.
4. **Worktree policy** — applied per step 4. NEVER destroys uncommitted work.

There is no `team_delete` row to enqueue and no `TeamDelete` handshake to drive — the host engine already reaps the workers. Abort's job is purely the durable record above.

### 4. Worktree preservation

The script NEVER destroys uncommitted work. If the current worktree is dirty (or its state cannot be read), it is ALWAYS preserved — on `--hard` it is preserved with a warning. A CLEAN worktree is auto-removed only on `--hard`, or on the soft path only when you pass `--clean-worktree`. The actual worktree decision is folded into the report's "what was torn down" section.

### 5. Graceful stop (soft path) — the TM-005 handshake (optional)

The soft path is graceful. If live teammates are still mid-task, the orchestrator MAY drive the TM-005 `shutdown_req` / `shutdown_resp` handshake on the **kept** `bridge_messages` wire first (each teammate echoes the `request_id` within one turn; see `internal/team-mode-rules/SKILL.md` TM-005) so workers wind down cleanly before the cycle stops. This handshake rides the message WIRE only — it is NOT tied to any dispatch-queue row, and abort no longer enqueues one. The `--hard` path skips the handshake entirely.

## Hard rules

- Abort is a recorder: it writes the postmortem + the `'aborted'` audit event + the status transition. The host engine reaps the workers — there is no team-delete step for the live session to service.
- Pass `--team-id` whenever you hold it; without it the `'aborted'` audit event (and thus resume detection) is SKIPPED.
- Never destroy a dirty worktree to force a clean teardown — preservation is intentional.
- `--db` defaults to `.ai/atelier.db`; pass no db path unless overriding the target.

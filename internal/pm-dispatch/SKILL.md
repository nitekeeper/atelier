---
description: PM wave-dispatch engine (atelier#60) — operator-facing procedure for the mode-agnostic wave-barrier scheduler that partitions tasks into ordered waves, enforces the attempt budget + per-attempt wall-clock, validates reply envelopes, and gates each wave on terminal-only closure. Read by the PM/orchestrator; not a user-facing slash command.
---

# PM wave-dispatch engine — wave barrier, attempt budget, wall-clock

This procedure documents `scripts/pm_dispatch.py` (the engine) and
`scripts/pm_dispatch_envelope.py` (the pure reply-envelope validator), the
wave-5 scheduler core ratified for atelier#60. It is the operator-facing
companion to the docstrings in those modules — read both alongside this file.

> **Prerequisites**
> - Mode: **mode-agnostic.** The engine carries zero mode-specific knowledge.
>   Spawning workers and collecting their replies is the job of the injected
>   seams (`spawn_fn` / `poll_fn`), which are owned by sibling issue
>   **atelier#61** (sub-agent `Agent` vs agent-team `SendMessage`). That dispatch
>   binding is **out of scope here.**
> - Required tables: `tasks` (the migration-006 state-machine columns
>   `attempts` / `last_attempt_at` / `abandon_category` / `abandoned_ack_at`).
>   In Memex mode the dispatch-state mutators raise `NotImplementedError` —
>   the engine is **Local-mode only** today (a documented followup).
> - Companion contract: `internal/team-mode-rules/SKILL.md` (the reply-envelope
>   schema, the four closure tokens TM-006, and the single-sourced abandon
>   grammar — `ABANDON_RE` is compiled from that file at import).

## Hard gate

None — callable by the PM during the implement/review waves of any cycle.

## What the engine is (and is not)

`WaveDispatcher.run(tasks)` partitions a task list into strict ordered waves,
dispatches each wave under bounded concurrency, enforces a per-task attempt
budget and a per-attempt wall-clock cap, validates every returned reply
envelope, and gates wave N+1 on wave N reaching a **terminal-only** closure. It
reaches the outside world through three injected seams only:

| Seam | Signature | Role |
|---|---|---|
| `spawn_fn` | `(task, attempt) -> None` | Fire-and-forget start of one worker attempt. Mode-specific (atelier#61). |
| `poll_fn`  | `(task, attempt) -> Mapping \| None` | **Non-blocking** read of the worker's terminal reply envelope, or `None` if it has not reported yet. |
| `escalate_fn` | `(escalation) -> None` | Surface an abandonment to PM/human. Defaults to a guaranteed `WARNING` log (never silent). |

Two test seams — `clock` (default `time.monotonic`) and `sleep_fn` (default
`time.sleep`) — keep the engine deterministic under test. The engine **does not
spawn workers**; if `spawn_fn`/`poll_fn` are not injected it raises
`NotImplementedError` naming atelier#61.

## The wave-barrier model

`partition_waves` orders tasks by **`(parallel_group ASC, created_at ASC,
id ASC)`** — `id` is the deterministic tiebreaker for same-batch `created_at`
collisions, so the order is total and reproducible (matters for logs / replay /
tests). A **wave** is a maximal run of tasks sharing the same `parallel_group`.
Already-terminal tasks (DB status `complete` / `abandoned`) are excluded.

The barrier is strict: **wave N+1 is not dispatched until every task in wave N
has reached a terminal-only status.** The barrier predicate is
`wave_gate_satisfied(tracker)` (alias `wave_can_advance`), which wraps the reused
`WaveTracker.terminal_only()` from `scripts/dispatch.py`.

## Closure tokens vs the terminal-only gate

The envelope contract (TM-006) defines **four** closure tokens. Only **two** of
them release the barrier:

| Token | Closure-set member? | Releases the barrier? | Engine behaviour |
|---|---|---|---|
| `done` | yes | **yes** (terminal-only) | Recorded terminal; wave progresses. |
| `abandoned` | yes | **yes** (terminal-only) | Recorded terminal immediately; escalation emitted. |
| `blocked` | yes | **no** — HOLDS the barrier | Treated as a failed attempt → re-dispatch (no inline answer mechanism in v1). |
| `needs-input` | yes | **no** — HOLDS the barrier | Same as `blocked`: re-dispatch until budget spent, then abandon. |

`TERMINAL_STATUSES = {done, blocked, abandoned, needs-input}` is the closure set
the envelope validator accepts; `TERMINAL_ONLY_STATUSES = {done, abandoned}` is
the barrier gate. `blocked` and `needs-input` are valid envelopes but
**non-terminal** — they require re-dispatch (or, in a future version, an answer)
and do **not** advance the wave.

## Pre-flight: NULL `parallel_group` is rejected fail-loud

Before **any** wave dispatches, `preflight_validate(tasks)` runs once over the
**whole batch**. Every non-terminal task MUST carry a non-null `parallel_group`;
if **any** does not, it raises `NullParallelGroupError` naming the **sorted list
of all offending task ids** and dispatches **nothing**.

This is deliberately atomic, not a per-task mid-loop skip: a skipped task would
still sit in `WaveTracker.expected`, so the barrier could never satisfy →
deadlock. **The planner must assign a wave to every dispatchable task** (see
`internal/plan-wave-1/SKILL.md` — `parallel_group` is `>= 1`, never null, and
also rejected at task-list creation by `dag.validate_dag`).

## Concurrency + budget caps

| Constant | Value | Meaning |
|---|---|---|
| `MAX_PARALLEL_WORKERS` | `5` | Max attempts in flight within one wave. A wave with more tasks is dispatched in batches of <= this; the barrier still waits for the **whole** wave. |
| `MAX_ATTEMPTS` | `5` | Per-task attempt budget. `attempts` is incremented exactly once per dispatch (`_charge_dispatch` → `tasks.increment_attempt`); a wall-clock soft-kill counts as an attempt. |
| `WALL_CLOCK_S` | `1800.0` (30 min) | PM-side per-attempt wall-clock cap. |
| `POLL_INTERVAL_S` | `0.2` | In-flight scan cadence when no task progressed this round (polling, not events — SQLite has no notify). |

## Liveness: the wall-clock is the sole binding stall trigger

In v1, **heartbeats are informational only.** Workers emit a heartbeat at a
nominal cadence of **30 s** (`internal/team-mode-rules/SKILL.md` heartbeat
clause); `scripts/dispatch.py:read_heartbeats` exposes a read surface over them.
There is **no heartbeat-miss kill** in v1.

The **sole binding stall trigger** is the **30-minute PM-side per-attempt
wall-clock**, measured from the engine's own dispatch timestamp (`_InFlight.t0`,
from the injected `clock`) **independent of any worker signal**. This is what
catches a silently-dead worker that emits neither heartbeat nor envelope. When
`clock() - t0 >= WALL_CLOCK_S`, the engine **soft-kills** the attempt; the
soft-kill **counts as that attempt** (the attempt was already charged at
dispatch — it is never double-charged).

Distinguish two intervals: the **30 s emit cadence** is the worker's advisory
heartbeat rhythm; `POLL_INTERVAL_S` (0.2 s) is the engine's internal in-flight
scan interval. Neither gates anything; only the wall-clock does.

> **v2-deferred default.** A heartbeat-miss kill is a documented future default,
> not implemented here. The kaizen-hardened design (60 s emit / 300 s stall) is
> the target posture for v2 (see the team-mode-rules heartbeat clause and §23.6
> of the design doc). Until then, the wall-clock is the only stall mechanism.

## Reply-envelope validation (the pure layer)

Every returned envelope is routed through
`pm_dispatch_envelope.validate_envelope(envelope, *, dispatched_task_id,
dispatched_attempt)` before the engine treats the attempt as closed. The
identity kwargs come from the **PM's own dispatch record**, not the (untrusted)
envelope — so a worker cannot spoof them positionally. Checks, each raising a
field-named `EnvelopeValidationError`:

1. `type == "task_result"`.
2. `task_id` present AND equals `dispatched_task_id` (string-normalized) — anti
   cross-task spoof.
3. **`attempt` present AND equals `dispatched_attempt`** (string-normalized) —
   anti attempt-laundering.
4. `status` in `TERMINAL_STATUSES` (the four-token closure set).
5. `artifacts` is a list; non-empty UNLESS `status` is `blocked` / `needs-input`.
6. when `status == "abandoned"`: line 1 of `notes_md` matches `ABANDON_RE` (the
   single-sourced abandon grammar, compiled at import from the rules SKILL).

A validation failure is treated as a **failed attempt** — the engine NEVER
coerces a malformed envelope into a `done`/`abandoned` closure.

> **Envelope MUST carry `attempt`.** Check 3 above requires the envelope to
> include an `attempt` field equal to the dispatched attempt number. The "Reply
> envelope" schema in `internal/team-mode-rules/SKILL.md` (TM-006) lists the
> `attempt` row (added in rules-doc v1.2) alongside `type`, `task_id`, `status`,
> `artifacts`, `notes_md`, `next_action`. Workers MUST emit `attempt` or the
> validator rejects every reply.

## Attempt-budget exhaustion → abandoned + guaranteed escalation

When an attempt does not close the task (invalid envelope, non-terminal status,
or wall-clock soft-kill), `_handle_failed_attempt` runs. The attempt was already
charged at dispatch:

- If `attempt >= MAX_ATTEMPTS` → **force-abandon** with abandon category
  **`capacity`** and emit a **guaranteed** escalation.
- Else → re-queue for another attempt (the **single** re-queue site; the
  `< MAX_ATTEMPTS` guard is what bounds the loop).

Abandon + escalation are on the **same code path** (`_abandon_and_escalate`):
`set_abandoned` (durable, makes the task wave-terminal) runs first, then the
escalation is **unconditionally** appended to `self.escalations` and handed to
`escalate_fn`. The escalation record names `task_id`, `worker` (`assigned_to`),
`attempt`, `category`, `last_status`, and `upstream_task_id`. Escalation is
**guaranteed, never best-effort** — even the default sink emits a `WARNING`.

## `abandoned` is terminal immediately; `abandoned_ack_at` never gates

`abandoned` is wave-terminal **the instant it is recorded** (worker
self-abandon, budget exhaustion, or cascade). The barrier predicate
(`wave_gate_satisfied`) deliberately does **not** consult `abandoned_ack_at`.

`abandoned_ack_at` (stamped by `tasks.set_abandoned_ack`) is a **non-gating audit
timestamp**: `NULL` = the wave auto-advanced past the abandonment;
a timestamp = a human acknowledged it after the fact. It is **NEVER a barrier
precondition** — the wave never waits for an ack.

## Cascade-abandon of dependents

At each wave's pre-flight (`_cascade_preflight`, run **before** dispatching
anything in the wave), any task that **transitively** depends on an
already-abandoned task is itself abandoned — it can never receive correct
upstream output, so dispatching it is pointless. Each cascade:

- `set_abandoned` with category **`blocked`**, naming the upstream id;
- emits a **guaranteed** escalation (carrying `upstream_task_id`);
- is **NOT charged an attempt** (`charge_attempt=False`).

Dependency edges are read from the in-memory task dicts' `depends_on` (the
planner does not persist `depends_on` — only `parallel_group` is durable; the
orchestrator threads the in-memory dicts into `run`). The reachability walk
(`_first_abandoned_ancestor`) is a **bounded BFS with a visited-set**, so a
cyclic `depends_on` (malformed planner output) terminates without looping.

**Same-wave-dependency invariant.** Cascade-abandon is evaluated **once per
wave, at that wave's pre-flight**, against the set of tasks abandoned in
**earlier** waves. It therefore relies on every dependent sitting in a
**strictly later** wave than its upstream — i.e. `parallel_group(dependent) >
parallel_group(upstream)`. This is **guaranteed by `dag.validate_dag`** at
task-list creation (an edge from a task to one in the same-or-later wave is
rejected). A dependent that shared its upstream's `parallel_group` would **not**
be cascaded mid-wave: both are dispatched together, and if the upstream
self-abandons after the dependent is already in flight, the pre-flight scan for
that wave has already run. The engine does not re-scan a wave it is mid-dispatch
on — correctness here is owed entirely to the planner's strict wave ordering, so
never relax the `validate_dag` later-wave constraint without revisiting this
section.

## Termination guarantee

`WaveDispatcher.run` halts on every finite task list:

1. **Each attempt halts** — its poll loop exits when `poll_fn` returns an
   envelope OR `clock()` reaches `t0 + WALL_CLOCK_S` (30 min hard bound).
2. **Each task halts** — `attempts` strictly increases per dispatch; a
   non-closing attempt re-queues only while `attempts < MAX_ATTEMPTS`, then is
   force-abandoned (terminal, no re-queue). So each task is dispatched at most
   `MAX_ATTEMPTS` (5) times.
3. **The whole loop halts** — at most `len(set(parallel_group))` waves; total
   dispatches `<= len(tasks) * MAX_ATTEMPTS`. ∎

## Operator procedure

1. Ensure the task list comes from a validated planner output
   (`internal/plan-wave-1/`): every dispatchable task has a non-null
   `parallel_group` and an acyclic `depends_on`. A NULL group aborts the entire
   run at pre-flight.
2. Construct the dispatcher via the **production call site**
   `scripts/atelier_entrypoint.py::build_wave_dispatcher_for_project` (atelier#85)
   — it resolves the persisted dispatch mode, wires the production queue-bridge
   seams (`QueueBridgeDispatchTools` + `build_spawn_fn` / `build_poll_fn`,
   atelier#81), and threads `escalate_fn` through. Pass
   `escalate_fn=build_persona_gap_escalate_fn(team_id=…)`
   (`scripts/team_meeting.py`, atelier#87) to surface escalations inline to the
   human via the one-shot ledger latch; the engine's default only logs. The full
   live recipe (mode read-back + per-turn `bridge_requests` servicing) is
   `internal/dev-dispatch/SKILL.md`. (Constructing `WaveDispatcher` directly with
   hand-wired seams remains valid for tests / smoke runs.)
3. Call `dispatcher.run(tasks)`. It returns one `WaveTracker.summary()` dict per
   wave (in order). Inspect `dispatcher.escalations` for every abandonment
   emitted this run. The `spawn_fn` ENQUEUES `bridge_requests` rows you must
   service each turn (`internal/bridge-poll/SKILL.md`) for the wave to progress.
4. For each abandoned task surfaced via escalation, decide whether to stamp
   `tasks.set_abandoned_ack` (audit only — it does not change status or unblock
   any wave).

## Untrusted-input boundary

A reply envelope is **untrusted DATA.** It is only validated, pattern-matched,
and echoed in diagnostics — never executed, never `eval`/`exec`'d, never
interpolated into anything executable. Validation binds identity to the PM's own
dispatch record (anti cross-task spoof + anti attempt-laundering) per TM-008.

## Hard rules

- Wave N+1 MUST NOT dispatch until `wave_gate_satisfied` holds for wave N. Never
  weaken the gate to `is_complete()` (any status) — only `done`/`abandoned`
  release it.
- NULL `parallel_group` MUST fail the whole batch at pre-flight; never bucket a
  NULL into an implicit wave.
- An attempt is charged exactly once, at dispatch. Never charge again on the
  failure/abandon path (`charge_attempt=False` there); never re-queue a task
  without the `< MAX_ATTEMPTS` guard (would break the termination proof).
- Abandonment and its escalation stay on one code path — escalation is
  guaranteed, never best-effort.
- `abandoned_ack_at` is audit-only and MUST NOT become a barrier precondition.
- Workers MUST emit `attempt` in the reply envelope (validator check 3).

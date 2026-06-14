---
description: Use when starting any session in a project that uses Atelier — establishes the trigger contract for new-work requests and the soft-wall bypass procedure.
---

<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific task (e.g., via `internal/dev-subagent/SKILL.md`) — identifiable by a briefing header such as "You are an implementer subagent" or "You have been dispatched to complete one task" — skip the Trigger contract below entirely. Apply the Bypass procedure normally; phase gates still govern your work. You are executing a bounded task within an already-active arc; do not re-initiate the Ask gate.
</SUBAGENT-STOP>

Atelier is a workspace and methodology for a human developer collaborating with one or more AI agents on a software project. This skill defines the trigger contract every session follows and the bypass procedure for soft phase walls.

> **Session-open requirement.** On the first message of every session in an Atelier project: verify Memex is present, identify the active project and its current phase, then select the phase-recommended procedure from Phase guidance before responding to any user request.

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

## Resume detection (after pre-flight)

In **Local mode only**, `startup_check()` may return a **`resume_offer`** field
ALONGSIDE its `action` token (it is additive — the `proceed-*` contract is
unchanged). The field is a `scripts.resume.ResumeOffer` carrying
`team_id`, `team_pk`, `project_id`, `abort_phase`, and `incomplete_count`. It
signals that a PRIOR team-mode arc was **aborted but left incomplete** (its
latest lifecycle audit event is `aborted`, not `completed`, AND ≥1 task is still
non-terminal). Resume detection is Local-only by design (§17): a Memex-mode run
has no Local team-mode dispatch state to resume, so `resume_offer` is never set
on the Memex branch.

**NEVER SILENT (§3 non-goal / §13).** The detector only OFFERS; it does NOT
force-phase, re-dispatch, or mutate any row. When `resume_offer` is present, the
PM MUST ask the human VERBATIM — substituting `abort_phase` and `incomplete_count`
— and WAIT for an answer:

> An aborted arc was found at phase `<abort_phase>` with `<incomplete_count>`
> incomplete task(s). New run or continue from where you left off? (new / continue)

- **On `continue`:** reuse the EXISTING project row + the persisted
  `tasks`/`bridge_messages` envelopes/`team_audit_log` — do **NOT** call the
  planner / `internal/dev-plan/SKILL.md` (no re-plan).
  1. **Resolve the integer `projects.id` FIRST.** `resume_offer.project_id` is
     the TEXTUAL `teams.project_id` correlation string (`TEXT`), NOT the integer
     `projects.id` PK that `force-phase` requires — these are two distinct values
     (a single project can host >1 concurrent team/cycle; `teams.project_id` has
     no UNIQUE constraint). Resolve the live project row via
     `scope.resolve_scope()` and read its integer id:
     ```
     python3 -c "from scripts import scope; print(scope.resolve_scope().project['id'])"
     ```
     Call the result `<project_rowid>` (an integer). Do NOT pass
     `resume_offer.project_id` into `force-phase` — `int('proj-7')` raises
     `ValueError`.
  2. **Force-phase to `<abort_phase>`** with the canonical CLI form (db_path is
     argv[1], the known command `force-phase` is argv[2], then the integer
     project_id and the phase) — this skips the transition graph:
     ```
     python3 scripts/workflow.py <db_path> force-phase <project_rowid> <abort_phase>
     ```
  3. **Re-enter `build_wave_dispatcher_for_project`**, whose `partition_waves`
     re-runs over the SAME `tasks` rows and dispatches ONLY the non-terminal ones
     (terminal `complete`/`abandoned` rows are dropped — the persisted
     tasks/envelopes are reused verbatim). The force-phase is the ONLY new write
     the resume performs.
- **On `new`:** leave the aborted arc intact (§13.1.3) and proceed as a fresh
  project — do not delete or rewrite the prior arc's rows.

If no `resume_offer` field is present, there is nothing to resume; continue to
Dispatch-mode selection.

## Settings recommendation (after pre-flight)

If `startup_check()` returned a `settings_rec_offer` with `eligible=True` and
non-empty `changes`, read `internal/settings-recommendation/SKILL.md` and follow
its prompt protocol BEFORE proceeding to the rest of this skill. After the
user's choice, continue the original command. The offer is mode-agnostic (it
fires on both `proceed-local` and `proceed-memex`); the procedure file is the
single source of truth for the prompt text (do not inline it here).

## Run mode selection — R-MODE (after pre-flight, before any work request)

Establish the session's **run mode** — a per-run cost/quality posture. R-MODE is **PER-RUN / transient**: it is resolved at run START, honored for the whole run, and **NEVER writes `~/.claude/settings.json`** (it is ORTHOGONAL to the once-per-version "Settings recommendation" flow above, which is the SOLE settings.json writer). The orchestrator model R-MODE surfaces is an **ADVISORY recommendation only** — a running session cannot change its own model mid-run.

The three modes (mapped onto `recommended_settings.PROFILES` — the single source of model families — in `scripts/run_mode.py`):

- **cost-lean** — cheapest posture: per-task tiers biased DOWN (toward haiku/sonnet), a tighter token budget, narrower fan-out. Maps to the `cost-effective` profile.
- **balanced** — neutral middle: no tier lean, default budget/fan-out. Maps to the `balanced` profile. (This is the byte-identical no-op posture.)
- **quality-lean** — quality posture: per-task tiers biased UP (toward opus), a looser budget, wider fan-out. Maps to the `code-quality` profile.

In every mode the **ROLE_FLOOR stays HARD**: a review / security / architect / safety role is ALWAYS opus, even under cost-lean.

**ALWAYS-PROMPT (interactive only).** When the session is interactive (a TTY, no CI marker, no `ATELIER_RUN_MODE` env pin), ALWAYS prompt the user at run start:

> *"Run mode for this run? **(1) cost-lean / (2) balanced / (3) quality-lean** — Enter for the saved default (`<DEFAULT_PROFILE>`'s mode). This is per-run only and never changes your settings.json; the orchestrator-model line is advisory."*

The Enter-default is the SAVED profile (`recommended_settings.DEFAULT_PROFILE`, currently `cost-effective` → `cost-lean`). Resolve the answer:

```python
from scripts.run_mode import resolve_run_mode
run_mode = resolve_run_mode(interactive_choice=<"cost-lean"|"balanced"|"quality-lean"|None>)
# Surface (do NOT write) the advisory orchestrator-model recommendation:
print(f"Advisory orchestrator model for this run: {run_mode.orchestrator_model} "
      f"(advisory only — R-MODE never writes settings.json).")
```

**NON-INTERACTIVE / CI — skip the prompt, never block.** When `CI` / `GITHUB_ACTIONS` is set, stdin is non-TTY, or `ATELIER_RUN_MODE` is pinned, do NOT prompt — call `resolve_run_mode()` (no `interactive_choice`) and it resolves silently: the `ATELIER_RUN_MODE` env pin if set, else the saved-profile default. A non-interactive run NEVER blocks on the prompt.

**Honor the resolved RunMode for the run.** Pass it to the host pipeline so the four levers (per-task posture, BudgetPool, fleet width, advisory orchestrator model) fan out:

```python
await run_host_pipeline_for_project(..., run_mode=run_mode)   # host/CLI transport
```

(The default `run_mode=None` inside `run_host_pipeline_for_project` calls `resolve_run_mode()` itself, so a caller that forgets to thread it still gets the env/CI-default behavior — but the always-prompt is THIS recipe's job and only fires here.)

## Dispatch-mode selection (after pre-flight, before any work request)

Once `startup_check` returns a `proceed-*` action and BEFORE the Trigger contract's Ask gate fires for any new-work request, establish the session's **dispatch mode**. **The default is sub-agent mode** — adopt it silently, with no live pick, unless the user explicitly chooses agent-team for this session or has pre-authorized agent-team in CLAUDE.md / saved preferences. This mirrors the read-side default in `scripts/dispatch.py::resolve_dispatch_mode` (env override → marker → `subagent` default).

- **sub-agent (default)** — lightweight. Each worker is a fire-and-forget background `Agent`; no persistent team is created. **tmux is NOT required.** Best for a single implementer / reviewer pass.
- **agent-team** — a persistent team (`TeamCreate` once per cycle, then per-task spawn / `SendMessage`). **tmux is REQUIRED** for the team panes. Selected only on explicit user request or pre-authorization.

Resolution:

1. **No explicit choice → sub-agent (default).** Proceed directly to step 3 with `subagent`; never run the tmux gate for sub-agent mode. (If the user has pre-authorized a mode in CLAUDE.md / saved preferences, treat that as the resolved choice with no live ask — skip to step 2 if it is agent-team, else to step 3.)
2. **agent-team (explicit) → tmux gate (hard-fail when unavailable).** When the user has chosen agent-team, check tmux availability first:
   ```
   python3 -c "from scripts.preflight import tmux_available; print(tmux_available())"
   ```
   - If it prints `False`: **STOP.** Do not silently proceed into agent-team. Tell the user verbatim: *"tmux is required for agent-team mode; install it and re-run, or choose sub-agent mode."* Then wait for the user to decide: install tmux and re-confirm agent-team, or proceed with the sub-agent default.
   - If it prints `True`: continue to step 3 with `agent-team`.
3. **Persist the resolved mode** so the later Python dispatch reads it back (env override → marker → default; see `scripts/dispatch.py::resolve_dispatch_mode`):
   ```
   python3 scripts/dispatch.py persist-mode <mode>
   ```
   where `<mode>` is exactly `subagent` or `agent-team`. This writes the `.ai/atelier.mode` marker. Then continue to the Trigger contract below.

This gate establishes HOW work is dispatched; the Trigger contract below still governs WHETHER a given message is new work. A user who pre-authorizes a mode in CLAUDE.md / saved preferences satisfies this gate without a live pick (same authority rule as the Ask gate).

## Internal procedures

Most dev-arc work and project CRUD lives in `internal/<name>/SKILL.md` files. These are NOT Claude Code slash commands — they are plain markdown procedures only reachable via the Read tool. Whenever this skill references `internal/<name>/SKILL.md` below, the agent should: (1) Read that file, (2) follow the procedure inline. The 22 internal procedures cover the dev arc (`internal/dev-design`, `internal/dev-plan`, `internal/dev-tdd`, …) and project DB CRUD (`internal/project`, `internal/task`, `internal/meeting`, …).

## Authority and override

User instructions override this skill's defaults at all times. If the user provides a direct instruction — "skip Atelier," "option (c)," "just do it," or any unambiguous directive to bypass this methodology — comply immediately without re-asking. This skill defines the default behavior for a session; it does not constrain the user's authority to change that behavior at any point.

Persistent instructions in CLAUDE.md or saved preferences that pre-authorize a routing choice satisfy this skill's Ask gate without a live confirmation per session.

"Skip Atelier entirely" is option (c) of the Ask gate — do not treat it as a conflict requiring bypass logging.

Priority order when instructions conflict:

1. **User's explicit instructions — highest priority.** Overrides this skill and all defaults.
2. **Atelier methodology (this skill).** Applies to all dev work unless the user has overridden.
3. **Default system prompt.** Applies only where neither the user nor this skill has spoken.

## Trigger contract

On every user message, before responding:

1. **Mid-arc rule.** If a project is active and its phase is not `handoff:complete`, continue the current arc. Do NOT ask. Proceed with the phase-recommended skill (see Phase guidance) or with the user's explicit request.
2. **No-fire rule.** If the message is a question, exploration, read-only request, or trivial edit (see Red Flags), handle directly without asking.
3. **Ask gate.** If the message describes new development work, ask the user one of three routings:
   - **(a) Full Atelier arc** — invoke `internal/project/SKILL.md` (`create`), then `internal/dev-design/SKILL.md`. Routes through design → plan → tdd → review → security → qa → handoff with soft walls.
   - **(b) Bug fix** — invoke `internal/dev-diagnose/SKILL.md` against the active project. Captures pre-diagnose phase, writes regression test first, restores phase on resolution.
   - **(c) Handle directly** — do the work without Atelier orchestration. No project created, no phase tracked.

   If you are uncertain whether a message describes new development work: apply the 1% principle — if there is even a 1% chance the message is a work request, ask the Ask gate question. The cost of asking once is lower than the cost of skipping Atelier for a substantive change.

Wait for an explicit user response. Default to (a) if the user says "yes" without specifying.

## Bypass procedure

Every dev skill's step 1 follows this pattern:

1. Call `python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> <skill>`. Parse the JSON output. The fields are: `allowed` (bool), `current_phase` (str), `required_phase` (str | null), `reason` (str).
2. **If `allowed` is true:** proceed with the skill's procedure.
3. **If `allowed` is false:**
   - Display to the user: *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*
   - On **yes:** call `python3 atelier/scripts/workflow.py <db_path> log-bypass <project_id> <skill> <current_phase> <required_phase>` (optionally with `--agent <agent_id>` and `--note "<reason>"`), then proceed with the skill's procedure.
   - On **no:** stop. Tell the user: *"Advance to `<required_phase>` first (run `python3 atelier/scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*

Bypass entries are recorded in the `phase_bypasses` table and surfaced by `internal/dev-handoff/SKILL.md` during retrospective.

## Red Flags

### Trigger-firing red flags

| Rationalization | Correct reading |
|---|---|
| "User just wants a quick fix" | Quick fixes still go through option (b). Ask. |
| "This is too small to need design" | Ask. User can pick option (c). |
| "User is asking a question, no need to ask" | Correct — questions don't fire. Only work requests fire. |
| "Project is already active, no need to ask" | Correct — don't re-ask mid-arc. Continue current phase. |
| "User said 'how do I X' so it's a question" | Verify: are they asking how, or asking the agent to do it? Latter fires. |
| "User said 'rename X to Y' — it's a tiny edit" | Tiny mechanical edits do not fire. Substantive renames (refactors affecting >5 files) fire. |
| "Refactor isn't new work" | Substantive refactors are new work. They get specs and reviews. Ask. |

### Mid-arc drift red flags

| Rationalization | Correct reading |
|---|---|
| "I already know this phase, I don't need to invoke the skill" | Skills evolve. The current skill file is the spec. Read it. |
| "The phase guidance says X but I know the right thing to do" | You are mid-arc. Follow the phase guidance. Surface conflicts — do not silently override them. |
| "Bypass-confirm-log is just overhead for obvious cases" | It is not overhead. It is the audit trail. Run the three-step flow or you have silently skipped a phase wall. |
| "The fix is obvious, TDD would slow this down" | Obvious fixes have the worst regression rate. Write the failing test first. The fix can be fast; the test cannot be skipped. |
| "Tests are passing, so I can skip directly to review" | Passing tests at tdd:green do not satisfy tdd:clean. Green is not clean. |
| "I'll verify later, it looks right" | "Looks right" is not evidence. Run `internal/dev-verify/SKILL.md` now. Later means never. |

**Firing patterns (examples):**
- "I want to add X" → fires
- "Build a system that does Y" → fires
- "The bug in Z is back" → fires (option b recommended)
- "Refactor the auth module" → fires
- "How does this codebase handle X?" → does not fire (question)
- "Show me the file at path Y" → does not fire (read-only)
- "Fix the typo on line 42" → does not fire (trivial edit)
- "List the open tasks" → does not fire (CRUD)

## Phase guidance

| Phase | Recommended next action | Skill |
|---|---|---|
| `design:open` | Continue grilling. Do not write code yet. | `internal/dev-design/SKILL.md` |
| `design:approved` | Draft the implementation plan. | `internal/dev-plan/SKILL.md` |
| `plan:open` | Continue refining the plan with the user. | `internal/dev-plan/SKILL.md` |
| `plan:approved` | Write the first failing test (single-agent). | `internal/dev-tdd/SKILL.md` |
| `plan:approved` (parallel tasks, **sub-agent** mode) | Dispatch fresh subagents per task with two-stage review instead of implementing directly. | `internal/dev-subagent/SKILL.md` |
| `plan:approved` (parallel tasks, **agent-team** mode) | Drive the live wave engine: `build_wave_dispatcher_for_project` + per-turn bridge-poll servicer; surfaces the meeting / side-query / roster / persona-gap-escalation behaviors. | `internal/dev-dispatch/SKILL.md` |
| `tdd:red` | Write minimal implementation to make tests pass. | `internal/dev-tdd/SKILL.md` |
| `tdd:green` | Verify tests pass (vacuity check, full output read), then refactor with tests still passing. | `internal/dev-verify/SKILL.md`, then `internal/dev-tdd/SKILL.md` |
| `tdd:clean` | Verify suite is clean, then continue TDD (new test) or advance to review. | `internal/dev-verify/SKILL.md`, then `internal/dev-tdd/SKILL.md` or `internal/dev-review/SKILL.md` |
| `review:open` | Address findings or mark as approved. | `internal/dev-review/SKILL.md` |
| `review:changes-requested` | Read all feedback, classify each item (accept / clarify / push-back), implement accepted fixes, re-request review. | `internal/dev-receive-review/SKILL.md` |
| `review:approved` | Run security review. | `internal/dev-security/SKILL.md` |
| `security:open` | Apply security findings or mark approved. | `internal/dev-security/SKILL.md` |
| `security:changes-requested` | Apply security findings, then re-review. | `internal/dev-security/SKILL.md` |
| `security:approved` | Run QA review. | `internal/dev-qa/SKILL.md` |
| `qa:open` | Address QA findings or mark approved. | `internal/dev-qa/SKILL.md` |
| `qa:changes-requested` | Apply QA findings, then re-review. | `internal/dev-qa/SKILL.md` |
| `qa:approved` | Run pre-flight checks, confirm CI green, choose integration path (merge / PR / abandon). | `internal/dev-finish/SKILL.md` |
| `handoff:open` | Integration artefact exists. Write session record and advance to complete. | `internal/dev-finish/SKILL.md` (step 5) |
| `diagnose:open` | Reproduce the bug, write regression test, fix root cause. | `internal/dev-diagnose/SKILL.md` |
| `diagnose:resolved` | Restore to pre-diagnose phase. | `internal/dev-diagnose/SKILL.md` (final steps) |
| `handoff:complete` | Project is closed. New work requires a new project. | — |

### Cross-cutting skills (any phase)

| Condition | Recommended next action | Skill |
|---|---|---|
| Before any phase advance where tests must pass | Run 5-step gate: identify tests, run suite, read full output, vacuity check, claim pass/fail. | `internal/dev-verify/SKILL.md` |
| Authoring new Atelier infrastructure | Author, review, and register a new skill. Does not require or advance any project phase. | `internal/dev-write-skill/SKILL.md` |

## Dev arc

The canonical Atelier development flow:

```
design → plan → tdd (red ⇄ green ⇄ clean) → review → security → qa → handoff
              ↑
              ├── dev-subagent  (sub-agent mode alternative to dev-tdd; enters at plan:approved, exits at tdd:clean)
              ├── dev-dispatch  (agent-team mode alternative to dev-tdd; live WaveDispatcher.run, enters at plan:approved)
              └── diagnose      (entered from any non-terminal phase, restored on resolve)
```

All transitions are tracked in `memex.db` (`projects.phase` column). Transitions are validated by `atelier/scripts/workflow.py advance` against the `phase_transitions` table. Skills no longer block on out-of-phase invocation — instead they apply the Bypass procedure above.

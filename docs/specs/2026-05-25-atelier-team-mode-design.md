# Atelier Team Mode — Design

**Status:** Draft — pending approval before plan-writing
**Date:** 2026-05-25
**Author:** nitekeeper (with Claude)
**Targets:** Atelier (this repo). No Memex-side changes required; consumer-only integration via existing `memex:run` plus the `~/.memex/atelier.db` durable backend established by the v1.1.0 retrofit.

---

## 0. Glossary and conventions

This spec uses several terms with project-specific meanings. The definitions here are normative — every later section conforms to them. If a section appears to drift, it's a spec bug; file a fix.

### 0.1 Core terms

| Term | Definition |
|---|---|
| **Team mode** | The two-shape execution model Atelier now operates in. Every `/atelier:run` invocation selects exactly one of: **sub-agent team mode** or **agent team mode**. There is no "no-team" path. (§4) |
| **Sub-agent team mode** | The lighter execution shape. Workers are fresh one-shot `Agent` tool invocations dispatched in waves, wrapped in a kaizen-style bridge_db helper. No tmux. No persistent team. (§10) |
| **Agent team mode** | The heavier execution shape. Workers are persistent team members created via `TeamCreate`. Tmux is mandatory; one pane per teammate. Cross-member meeting is supported in plan phase. (§9) |
| **PM** | Atelier itself, acting in the Project Manager role. Pure orchestrator — never performs implementation work directly. The sole human-facing surface during a run. (§5) |
| **Worker** | A subordinate execution unit dispatched by PM. In sub-agent mode = a fresh `Agent` invocation; in agent team mode = a team member instantiated under the `TeamCreate` umbrella. (§5.2) |
| **Wave** | A `parallel_group` integer assigned by the planner on every task. Tasks in the same wave dispatch in parallel; the next wave is gated on completion of the previous one. Strict and mandatory in both modes. (§5.4) |
| **bridge_db** | Atelier's transient signal bus: pings, async questions, abort broadcasts, heartbeats. Channel-scoped. Shipped with `scripts/bridge_send.py` + `scripts/bridge_read.py` helpers, ported from kaizen. (§14.1) |
| **Durable backend** | The persistence layer for artifacts: spec, plan, task list, meeting minutes, phase result docs, failure reports. Routes to Memex mode or local mode per atelier's existing dual-mode persistence (see [2026-05-16-atelier-memex-v2-retrofit-design.md](2026-05-16-atelier-memex-v2-retrofit-design.md)). (§14.2) |
| **Atelier-rules** | The versioned hard-rules file at `internal/team-mode-rules/SKILL.md`. PM's dispatch script reads it and prepends it to every worker briefing. The worker output envelope records the `rules_version` it operated under. (§16.2) |
| **Briefing** | The composed prompt PM hands to each worker at dispatch. Composition order is fixed (§16.3). Briefing is not free-form — it is assembled from atelier-rules + persona profile + phase procedure + task block + output requirements + bridge_db wiring + self-verify protocol. |
| **Self-verify** | The mandatory pre-"done" check every worker runs: project CI (hard correctness) AND phase/persona checklist (soft quality). 5-attempt fix-loop budget plus a token/wall-clock cap. On exhaustion the worker writes a failure report and escalates to PM. (§5.2) |
| **Open-questions list** | **Transient, per-worker.** The explicit running list a single worker maintains during its run. Lives in the worker's working memory; resolved before "ping done." Different from **Risks / Unknowns** — that is a **persistent section 8 of the 9-section spec** (§6.2) capturing project-level unknowns the spec author cannot resolve. A worker's open question may, as one of its three resolution paths, be promoted into the spec's Risks / Unknowns section; the worker's own list is then closed. (§5.3) |
| **No-guessing rule** | Search priority for any factual question a worker needs answered: durable backend → internet → own training → explicit-confidence assumption (with `N%` confidence label). Never silent. (§5.3) |
| **Phase** | One of `plan`, `tdd`, `implement`, `review`, `security`, `qa`, `finish`. Plus the orchestrator phase `pm` that precedes them. All seven dev phases run for every project, including trivial ones — no shortcuts. (§5.1) |
| **finish phase** | The seventh dev phase, renamed from "handoff" to match atelier's existing `internal/dev-finish/SKILL.md`. Atelier's existing `dev-handoff` (session-state DB record) still fires automatically after `finish` completes. (§5.1) |
| **Worker output envelope** | The structured JSON shape every worker emits when reporting done. Required fields are defined in §14.4. Missing any field blocks "ping done." |
| **Side-query** | A direct human-to-worker exchange in agent team mode via the worker's dedicated tmux pane. Soft relaxation of the "human only talks to PM" rule. Workers log every side-query to the durable backend so PM retains full context. (§9.4) |
| **9-section spec template** | The required structure PM enforces during the PM phase: Goal, Scope, Non-goals, Acceptance criteria, Constraints, Stakeholders, Dependencies / Prerequisites, Risks / Unknowns, Success metrics. Stored as `domain=project, subdomain=spec`. (§6) |
| **Spec amendment** | A new version of the spec document or task list, written as a fresh row with `metadata.version=N` and `metadata.supersedes=<prior_doc_id>`. Latest version wins; history preserved. (§14.3) |
| **Worktree** | A git worktree (one per implementer in a wave) isolating parallel implementers from each other. Implementer workers push to their worktree's remote branch; finish phase merges into the feature branch. (§12) |

### 0.2 Naming and conventions inherited

All path conventions, slug rules, naming conventions, and identifier patterns are inherited unchanged from [2026-05-16-atelier-memex-v2-retrofit-design.md §0.2](2026-05-16-atelier-memex-v2-retrofit-design.md). New identifiers introduced in this spec:

- bridge_db channel IDs use the format defined in §14.1.
- Feature branch slug: `atelier/<project-slug>` (project slug per the v1.1.0 retrofit rules).
- Failure report key (durable backend): `<workspace_slug>/<project_slug>/postmortem/<date>-<failure-context>-<seq>`.

---

## 1. Context and motivation

Atelier's pre-team-mode shape dispatched workers ad-hoc via the legacy `dev-*` phase procedures with no explicit parallelism, no shared rules surface, and no formal escalation contract. Three forces are pushing this redesign:

1. **Kaizen's multi-cycle dispatch validated wave-based parallelism end-to-end.** Kaizen runs (PRs #21, #30, #31, #35 — see Memory) shipped the bridge_db + worker-envelope + wave-dispatch primitives, proved they work, and surfaced the failure modes (Memory: "CC team-mode is async-only", "TeamDelete is per-session", "reviewer catches implementer misses"). Atelier is the right place to consolidate them.
2. **Atelier#34 was filed in 2026-05-23 to reintroduce `tasks.parallel_group` but had no consumer.** This design IS that consumer. Wave-based dispatch becomes mandatory; the planner cannot leave `parallel_group` null.
3. **Two-mode coexistence is unavoidable.** Some runs warrant the cost of a persistent team with a shared meeting (deep architecture work, exploratory designs); most runs do not (small features, well-scoped fixes). Bolting on an opt-in team mode would leak conditionals through every phase procedure. Picking a default and forcing the other shape into a parallel codepath bifurcates atelier. Forcing a choice up front and reusing the same phase procedures for both shapes — via dispatch-layer composition, not phase-level conditionals — is the cleaner contract.

The redesign is also a forcing function for atelier's hard rules. The "no silent deferrals" and "no guessing" rules existed only in personal CLAUDE.md (Memory: "Personal Rules"). Moving them into `internal/team-mode-rules/SKILL.md` makes them part of the product, applied to PM and every worker uniformly.

## 2. Goals

1. **Single entry, two shapes.** `/atelier:run` is the only invocation. Mode is asked at session start with no default. Workers are uniform consumers of the same phase procedures regardless of shape.
2. **PM as broker.** Human ↔ PM ↔ workers. The human never talks to a worker except through PM (with the agent-team-mode side-query relaxation in §9.4). PM never implements; PM only dispatches, answers, and escalates.
3. **Wave-based dispatch is mandatory.** Every task carries a `parallel_group` integer. PM strictly waits for wave N to finish before dispatching any task in wave N+1.
4. **No silent deferrals; no guessing.** Two hard rules that apply equally to PM and every worker, enforced via the versioned atelier-rules file and the briefing-composition pipeline.
5. **Reuse, don't fork.** The existing `internal/dev-*/SKILL.md` procedures (the dev-arc procedure set shipped by atelier) stay as-is, with one update: `dev-design` adopts the 9-section spec template — see §6 and §16.5 for how `dev-design` relates to the human-facing PM phase. Mode-specific behavior is layered in PM's dispatch briefing, never inside the phase procedures.
6. **Existing 61-role roster meets the persona bar.** No roster rework. PM instantiates existing roles per task; new personas can be registered globally with human confirmation.
7. **Memex mode and local mode both work.** The durable-backend abstraction from the v1.1.0 retrofit handles routing. Team mode is mode-agnostic.

## 3. Non-goals

- Multi-machine team coordination. A `/atelier:run` invocation is bound to one machine.
- Multi-human teams. One human + PM + workers.
- A "no team" execution mode. Every run is a team run; the question is which shape.
- Mid-run mode switching. The shape choice at session start is locked for that run.
- Cross-run team reuse. Each `/atelier:run` creates and tears down its own team (agent team mode) or its own dispatch context (sub-agent team mode).
- Persisting agent-team-mode panes across human sessions. If the human closes the tmux session, the panes go with it; orphan sweep (§9.5) cleans up team rows.
- Mid-phase persona swap. Once the planner assigns `assigned_persona` per task, dispatch honors it. Replacement only on failure-report-driven re-dispatch.
- Automatic resumability without human prompt. PM always asks "new or continuation?" — never silently resumes (§13).

## 4. Architecture overview

### 4.1 The two shapes side by side

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                              /atelier:run                                        │
│                "sub-agent team mode or agent team mode?"                         │
└──────────────────────────────────┬───────────────────────────────────────────────┘
                                   │
                                   ▼
                        ┌───────────────────────┐
                        │  pm  (9-section spec) │     ← shared pre-phase
                        └───────────┬───────────┘
                                    │
                  ┌─────────────────┴─────────────────┐
        sub-agent team mode                 agent team mode
                  │                                   │
                  ▼                                   ▼
         ┌──────────────────┐                ┌──────────────────┐
         │  plan:           │                │  TeamCreate      │
         │    wave-0        │                │  (persistent     │
         │    specialists   │                │   team, tmux)    │
         │      ↓           │                │       │          │
         │    wave-1        │                │       ▼          │
         │    planner       │                │  plan: meeting + │
         │  (one-shot Agent │                │   planner output │
         │   calls)         │                │                  │
         └────────┬─────────┘                └────────┬─────────┘
                  │                                   │
                  └─────────────────┬─────────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  tdd → implement →      │     ← shared dev-arc
                       │  review → security →    │       (phases 2..7)
                       │  qa → finish            │
                       │                         │
                       │  Wave-based dispatch    │
                       │  in both modes (§5.4)   │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  dev-handoff (auto)     │     ← shared coda
                       └────────────┬────────────┘
                                    │
                  ┌─────────────────┴─────────────────┐
        sub-agent team mode                 agent team mode
                  │                                   │
                  ▼                                   ▼
         (sub-agents already exited;          ┌─────────────────────┐
          no team to tear down)               │  TeamDelete         │
                                              │  + tmux teardown    │
                                              │  + team_runs.json   │
                                              │    entry removed    │
                                              └─────────────────────┘
```

### 4.2 Phase flow (both modes)

The `pm` pre-phase orchestration spine precedes the 7 numbered dev-arc phases. `dev-handoff` is a coda that fires automatically after phase 7 (`finish`). All phases run for every project — no shortcuts for "trivial" work.

```
[pre]  pm
         │
[1..7]   ├─→ plan ─→ tdd ─→ implement ─→ review ─→ security ─→ qa ─→ finish
                                                                       │
[coda]                                                                 ▼
                                                              dev-handoff (auto)
```

- **`pm` (pre-phase)** — atelier-as-PM interviews the human, captures the 9-section spec. No workers dispatched.
- **`plan` (phase 1)** — planner produces task list with assigned_persona + dependencies + wave per task. In agent team mode this includes a team-wide meeting; in sub-agent team mode it is a parallel-specialists pass followed by a planner synthesis pass.
- **`tdd → implement → review → security → qa → finish` (phases 2–7)** — PM dispatches workers per the task list, wave by wave. Reviewer/security/qa workers are dispatched separately with independent personas. No meetings after plan phase in either mode.
- **`dev-handoff` (coda)** — atelier's existing session-state-record procedure, fires automatically after `finish` completes (unchanged from current behavior).

### 4.3 What lives where (mode-specific bits)

| Surface | Sub-agent team mode | Agent team mode |
|---|---|---|
| Worker invocation | Fresh `Agent` tool call per task | Team member via `TeamCreate` |
| Persistence between phases | None (each Agent call is one-shot) | Team members persist for the run |
| Plan-phase meeting | Replaced by parallel specialist reads + planner synthesis | Structured bridge_db thread on `team-meeting` channel |
| Tmux requirement | None | Mandatory (hard fail if not in tmux) |
| Teardown | Implicit (no team) | Explicit `TeamDelete` + orphan-sweep procedure |
| Human → worker side-queries | Not supported | Supported via worker's tmux pane (§9.4) |
| Parallel cap | `MAX_PARALLEL_WORKERS=5` default (configurable) | Bounded by TeamCreate capacity + tmux pane budget |

### 4.4 What is shared

- The 7 dev phases use the same `internal/dev-*/SKILL.md` procedures unchanged (except `dev-design` updated to the 9-section template).
- The atelier-rules file (`internal/team-mode-rules/SKILL.md`) is prepended to every worker briefing identically.
- bridge_db channel naming (§14.1) is identical.
- Worker output envelope (§14.4) is identical.
- Wave-based dispatch contract is identical.
- The 61-role persona roster is the same.
- Durable backend routing (Memex / local) is the same.

## 5. Common workflow

### 5.1 Phases

Every `/atelier:run` traverses one **pre-phase orchestration spine** (`pm`) followed by **7 numbered dev-arc phases**. After phase 7 (`finish`), atelier's existing `dev-handoff` procedure fires automatically as a coda — it is not itself a numbered phase.

| # | Phase | What runs | Output |
|---|---|---|---|
| pre | `pm` | 9-section spec interview between human and PM (§6). Pre-phase orchestration spine — no workers dispatched. | Spec doc (`domain=project, subdomain=spec`) |
| 1 | `plan` | Planner (with meeting in agent team mode; with parallel specialists + planner in sub-agent team mode) | Meeting minutes (agent mode only) + task list rows in `tasks` |
| 2 | `tdd` | Test-first specialists per task in this phase | Test files committed via worktrees |
| 3 | `implement` | Implementers per task; parallel within wave | Code commits via worktrees |
| 4 | `review` | Independent reviewer per implementer task | Review docs (durable backend) |
| 5 | `security` | Security engineer per scope unit | Security report docs |
| 6 | `qa` | SDET per scope unit | QA report docs |
| 7 | `finish` | Finish worker collates, opens PR | Merged worktrees + feature-branch PR |
| coda | `dev-handoff` | atelier's existing handoff procedure, fires automatically after `finish` | Session-state DB record |

No phase is skippable. The "no shortcuts even for trivial fixes" rule means a one-line typo fix still passes through tdd, implement, review, security, qa, and finish — each phase will be fast for trivial work, but the chain is fixed.

### 5.2 PM and worker behavior

**PM behavior.**

- PM = atelier acting as project manager. Pure orchestrator. Never does implementation work directly.
- PM = bidirectional broker: human ↔ PM ↔ workers. The human's only interface is PM (subject to the §9.4 side-query relaxation).
- PM dispatches workers per the planner's task list, monitors progress via bridge_db, and escalates failures to the human.
- PM answers worker questions when it can. When it can't, PM escalates to the human and relays the answer back to the worker.
- PM emits proactive milestone updates inline to the user (wave start, wave complete, escalations, finish-phase completion).

**Worker behavior.**

- Worker = sub-agent (sub-agent team mode) or team member (agent team mode).
- Async messaging only. Workers fire questions to PM via bridge_db and keep working. They do not block on PM responses.
- Self-verification before pinging done: project CI (hard correctness) AND phase/persona checklist (soft quality). Both must pass.
- Self-verify uses a **5-attempt fix-loop budget** plus a wall-clock and token cap per attempt. Defaults: **wall-clock 30 min**; **token cap deferred to plan-writer** (placeholder: rely on the underlying `Agent` / team-member tool's own context-window limits as a hard ceiling until a concrete atelier-side cap is decided — see §23.11). Both wall-clock and token caps are configurable.
- On exhaustion: worker writes a failure report to the durable backend (`domain=postmortem, subdomain=failure`) and pings PM. PM then escalates to the human.
- Reviewer / security / qa workers are dispatched **separately** with independent personas. The planner enforces independence (no same persona reviewing its own task); PM verifies at dispatch time.
- Workers populate the output envelope (§14.4) before reporting done. Missing fields block "ping done."

**Wave-based dispatch (both modes, mandatory and strict).**

- Every task has a `parallel_group` integer assigned by the planner. Null is rejected at task-list creation time.
- PM dispatches every task in wave N concurrently (subject to `MAX_PARALLEL_WORKERS` in sub-agent mode).
- PM blocks before dispatching wave N+1 until **every** task in wave N has reached a terminal envelope status — `done` (with passing envelope) or `abandoned` (with human acknowledgement recorded). `blocked` is non-terminal and does NOT release the wave gate. (Status enum + transitions: §14.4.)
- A task that exhausted its 5-attempt budget and was abandoned does NOT block the wave indefinitely; PM marks it as abandoned in the durable backend and proceeds (with explicit human acknowledgement of the abandonment).

### 5.3 Hard rules (PM and all workers)

These ship in atelier itself (in `internal/team-mode-rules/SKILL.md`), not in personal CLAUDE.md. They apply uniformly to PM and to every worker via the briefing-composition pipeline.

**No silent deferrals.**
- Every worker maintains an explicit **open-questions list** (transient, per-worker — see Glossary §0.1) throughout its run.
- Before pinging done, the worker MUST resolve every open question by one of three paths:
  1. PM answers (via bridge_db).
  2. PM escalates to human; answer relayed back.
  3. The question is promoted into the **Risks / Unknowns** section of the spec (the persistent section 8 per §6.2) with rationale for why it cannot be resolved now. The worker's local list entry is then closed (the unknown lives on at spec level for the next dispatch wave or future runs to consume).
- Silent abandonment of an open question is a self-verify failure.

**No guessing.**
- When a worker needs a fact it doesn't have, search priority is **strict and explicit**:
  1. Durable backend (Memex search / local FTS5).
  2. Internet (when worker has web-fetch capability — see below).
  3. Worker's own training data.
  4. Explicit-confidence assumption — recorded in the output doc with an `N%` confidence label (e.g. `[assumption — 65% confidence]`).
- **Workers without web-fetch capability skip step 2 and proceed directly to step 3.** This is the common case for sub-agent-mode workers, whose tool allowlist may not include web-fetch. The skipped step is logged in the worker's output doc (a short note: `[step 2 skipped — no web-fetch in this worker's allowlist]`) so the human / PM can see whether a fact might have been found online if the capability were available. If a worker repeatedly needs web-fetch and lacks it, PM may escalate to the human to expand the allowlist for the next dispatch.
- Silent inference (e.g. "I'll just assume the API returns X") is forbidden. Every assumption is labeled.

### 5.4 Wave-based dispatch — the consumer for atelier#34

Atelier#34 reintroduced `tasks.parallel_group`. Prior to this design it had no consumer. This design makes it the central dispatch primitive:

- Planner sets `parallel_group` per task at task-list creation time. Not null. Enforced by the planner's briefing.
- PM's dispatch script (`scripts/dispatch.py`) reads tasks ordered by `(parallel_group ASC, created_at ASC)`.
- PM's dispatch loop: for each unique `parallel_group` value in ascending order, dispatch all tasks in that group concurrently, await all, advance.
- **Wave gates are strict and global — no overlap.** Wave N+1 begins only after every task in wave N has reached a terminal state (done or abandoned). The `parallel_group` ordering is the single source of truth; the dispatcher does NOT cross-reference `phase` to decide ordering. The planner is responsible for assigning wave numbers consistent with the dependency graph (e.g., tdd tasks get an earlier wave than the implement tasks that depend on them). If the planner chooses to place tasks of different phases in the same wave (only valid when no dependency exists between them), the dispatcher honors the wave assignment and dispatches them in parallel.

## 6. PM phase

### 6.1 Shape

A 1-on-1 between the human and atelier-as-PM. No workers dispatched. No team created.

PM walks the user **section by section** through the 9-section template, grilling for specificity. The PM phase is the most important phase for downstream correctness — every later worker reads the spec from their field's perspective, and a vague spec produces vague work.

### 6.2 The 9-section spec template

| # | Section | Purpose | Specificity bar |
|---|---|---|---|
| 1 | Goal | Single-sentence outcome statement | Testable; no compound goals |
| 2 | Scope | What is in | Each scope bullet is concretely demonstrable |
| 3 | Non-goals | What is explicitly out | Each non-goal closes a likely interpretation ambiguity |
| 4 | Acceptance criteria | How "done" is measured | Each criterion is testable (boolean or measurable) |
| 5 | Constraints | What we may not do (perf, compat, etc.) | Named, not hand-waved |
| 6 | Stakeholders | Who cares + their interest | Named role (not "the team") |
| 7 | Dependencies / Prerequisites | What must exist before we can ship | Named systems / artifacts / decisions |
| 8 | Risks / Unknowns | What we don't know + what could blow up | Each risk has a mitigation or an "accept" disposition |
| 9 | Success metrics | How we know it worked after ship | Quantified or boolean |

### 6.3 Completion criterion (hybrid)

PM declares the spec complete only when **all three** conditions hold:

1. All 9 sections pass the specificity bar (acceptance criteria are testable, etc.).
2. PM has zero open or deferred questions remaining (the no-silent-deferrals rule).
3. The human explicitly confirms "no more to add."

PM MAY refuse a premature "I'm done" from the human if sections fail the specificity bar. The refusal is explicit, with a list of failing sections and what's missing per section.

### 6.4 Storage

Stored to the durable backend as `domain=project, subdomain=spec`. Body is structured markdown with one header per section. The `project` row referenced by the spec doc is the project created (or resumed) by `resolve_scope()` at PM-phase start — `resolve_scope()` is defined in [2026-05-16-atelier-memex-v2-retrofit-design.md §10.2](2026-05-16-atelier-memex-v2-retrofit-design.md) (workspace from CWD; project from per-workspace session state with auto-select / prompt fallbacks).

## 7. Plan phase — agent team mode

### 7.1 Sequence

After the PM phase completes, agent team mode runs:

1. PM creates the persistent team via `TeamCreate` (composition inferred from spec — see §11.2).
2. PM informs the team: spec doc ID + bridge_db channel IDs + atelier-rules version.
3. Implementation team opens a meeting session.
4. Planner facilitates.
5. Meeting produces meeting minutes + a task list.

### 7.2 Meeting shape

- **Structured bridge_db thread.** The team uses the `run-<project_id>:team-meeting` channel. Messages are team-wide visible — every member reads every other member's messages.
- **Field-perspective reads.** Planner prompts each member to read the spec from their field's perspective (security engineer reads for security exposure, SDET reads for testability, architect reads for design coherence, etc.).
- **Async questions to PM.** During the meeting, any member may async-ask PM via bridge_db for spec clarifications. PM answers (or escalates to the human and relays). The meeting does not block on PM responses.
- **Persona-gap mid-meeting.** If a gap surfaces ("we need a security-engineer for this auth work, but the team doesn't have one"), PM may either grow the team (allowed as orchestration — see §11.3) OR escalate to the human. The choice is PM's, informed by whether the gap fits a roster role (grow) or requires a new persona (escalate).
- **Planner declares done.** The meeting ends when the planner declares it done.
- **Backstop.** Wall-clock cap (default 60 min) and message-count cap (default 200 messages per team-meeting channel) force termination on spiral. On backstop trigger, planner is forced to declare the meeting done with whatever state was reached.

### 7.3 Meeting failure

If the meeting cannot produce a task list (no consensus, planner cannot synthesize):

- Planner writes a meeting-failure report to the durable backend (`domain=postmortem, subdomain=meeting-failure`).
- PM one-shot-escalates to the human. No auto-retry.
- Human chooses: amend spec and rerun PM phase, force-resume with a planner override, or abort.

### 7.4 Task list output

After the meeting, planner produces two artifacts:

1. **Meeting minutes** — durable backend, `domain=meeting, subdomain=plan`. Captures discussion summary, decisions reached, alternatives considered, and the rationale for the task breakdown.
2. **Task list** — rows in `tasks` (one row per task) with these fields planner-enforced:
   - `assigned_persona` (must match an existing agents.db row, OR a newly registered persona — see §11)
   - `parallel_group` (wave; integer; not null)
   - `dependencies` (list of upstream task_ids; the wave assignment must be consistent with the dependency graph)
   - `phase` (one of `tdd`, `implement`, `review`, `security`, `qa`, `finish`)
   - `description` (task-specific block consumed in the briefing)
   - **reviewer-independence enforcement** — the persona assigned to a `review` task MUST differ from the persona assigned to the `implement` task it reviews. Planner enforces; PM re-verifies at dispatch.

## 8. Plan phase — sub-agent team mode

### 8.1 Sequence

After the PM phase completes, sub-agent team mode runs in two **plan-phase orchestration waves**. These are pre-task-list orchestration steps used to *build* the task list; they are NOT `tasks.parallel_group` values (which only exist on rows in the task list, which doesn't exist yet during the plan phase):

1. **Plan-phase wave 0 — parallel specialist reads.** PM infers a specialist persona set from the spec (typically 3–7 personas). PM dispatches all of them in parallel via the `Agent` tool (single message, N background calls with `run_in_background=true`). Each specialist sub-agent reads the spec from its field's perspective and writes a field-analysis doc to the durable backend (`domain=research, subdomain=field-analysis`).
2. **Plan-phase wave 1 — planner synthesis.** Once all wave-0 sub-agents complete, PM dispatches a single planner sub-agent. Briefing includes: spec doc + every field-analysis doc. Planner produces the task list (with its own `parallel_group` values per task, consumed by phases 2–7 dispatch per §5.4).

### 8.2 Why no meeting

Sub-agent team mode is positioned as the **lighter-weight alternative**. A meeting requires persistent members able to read each other's messages — which is precisely the agent-team-mode shape. Sub-agent mode trades the meeting benefit for cost + simplicity. If the user wants the meeting (discussion, mutual challenge, cross-discipline argument), they pick agent team mode at the entry prompt.

### 8.3 Failure modes

The locked rule is symmetric across modes: **synthesis-failure → one-shot escalate, no auto-retry; worker failures follow the common-workflow 5-attempt budget (§5.2).**

- **A specialist (wave-0 worker) fails** — this is a normal worker failure under common workflow. The 5-attempt budget applies; if the budget is exhausted, the worker writes a failure report (`postmortem/failure`), `tasks.status='abandoned'`, and PM escalates to the human per §5.2. The wave does not block indefinitely.
- **Planner fails to synthesize** — planner writes a synthesis-failure report (`postmortem/meeting-failure`); PM one-shot-escalates. No auto-retry. (Symmetric with §7.3 meeting-failure handling.)
- **Specialist set comes back insufficient** — if the human determines after escalation that the specialist coverage was wrong, the human chooses: amend spec and rerun PM phase, force-resume with degraded inputs, or abort.

### 8.4 Task list output

Identical to agent team mode (§7.4) — same `tasks` rows, same enforced fields, same reviewer-independence rule. The only difference is the absence of meeting minutes.

## 9. Agent team mode — specifics

### 9.1 Team creation

- PM creates the persistent team via `TeamCreate` after the PM phase completes.
- Composition is PM-inferred from the spec. All personas are instantiated up front (no mid-run growth except via §11.3 orchestration).
- **Strict-fixed after the plan-phase meeting.** Once the meeting completes and the task list is produced, the team membership is frozen for the remainder of the run.
- Reviewer / security / qa are normal team members. Independence is enforced by separate-worker dispatch (review task → not the same persona that did the implement task).

### 9.2 Teardown

- **Explicit `TeamDelete`** at the end of the finish phase.
- PM also tears down on abort (§13).
- **Orphan sweep.** Atelier ships an orphan-sweep procedure (modeled on kaizen's `sweep_leaked_teams.py` — see Memory: "CC TeamDelete is per-session") at `scripts/sweep_leaked_teams.py`. It runs on `/atelier:run` start, sweeping stranded teams from crashed or interrupted prior sessions. Per-session `TeamDelete` limitation means cross-session orphans need filesystem cleanup; the sweep handles that.

### 9.3 Tmux

- **Hard fail** if `/atelier:run` is invoked in agent team mode and the process is not inside a tmux session. The error message tells the user how to start a session (`tmux new -s atelier`, then re-run).
- **Layout.** PM pane fixed at left 1/3. Workers tile the right 2/3 in a 2D arrangement (atelier drives the split commands; layout management is automatic).
- **3 keybindings**, installed on first agent-team-mode `/atelier:run` after a one-time consent prompt:

| Binding | Action |
|---|---|
| `Prefix + p` | Focus the PM pane |
| `Prefix + w` | Cycle through worker panes |
| `Prefix + 1` … `Prefix + 9` | Jump to worker N |

- **Install mechanics.**
  - Atelier writes `~/.config/atelier/tmux.conf` (the atelier-owned config).
  - Atelier adds a one-line `source-file ~/.config/atelier/tmux.conf` include to `~/.tmux.conf` (the user's tmux config), with a backup written to `~/.tmux.conf.bak.<iso8601>`.
  - **Idempotent** — re-running detects the existing include line and is a no-op. Consent prompt fires only once per machine.

### 9.4 Side-queries

The "human only talks to PM" rule is softly relaxed in agent team mode. The human MAY directly side-query a worker via the worker's dedicated tmux pane:

- Side-queries are **async and opportunistic** — the worker reads them when it can, not as a blocking interrupt.
- The worker logs every side-query (prompt + response) to the durable backend (`domain=log, subdomain=side-query`).
- PM retains full context because PM reads the log.
- Side-queries do NOT replace PM-mediated escalation. They are for quick clarifications and observation, not for redirecting the worker.

### 9.5 Orphan sweep procedure

Atelier tracks live agent-team-mode runs via a dedicated artifact, NOT via a hidden column on `projects`:

- **Tracking artifact: `~/.atelier/team_runs.json`.** A small JSON file maintained by atelier on the local machine. One entry per active agent-team-mode run:
  ```json
  {
    "<team_id>": {
      "project_id": <int>,
      "run_id": "<atelier run id>",
      "pid": <int>,
      "tmux_session": "<session name>",
      "team_dir": "~/.claude/teams/<team_id>",
      "started_at": "<iso8601>"
    }
  }
  ```
- The entry is written at `TeamCreate` time and removed at `TeamDelete` time. Lives outside the durable backend so it survives Memex/local-mode boundary changes and is cheap to read on every `/atelier:run` start.
- **Orphan team directory** = the per-team filesystem state Claude Code writes for an agent-team session. Per Memory ("CC TeamDelete is per-session — cross-session orphan recovery needs filesystem `rm -rf ~/.claude/teams/<team_id>/`"), the canonical path is `~/.claude/teams/<team_id>/`. The exact path is stored in the `team_dir` field of the tracking artifact rather than hardcoded, so future Claude-Code changes to the path layout do not break the sweep.

At `/atelier:run` start, before any team creation:

1. Read `~/.atelier/team_runs.json`.
2. For each entry, check whether `pid` is still alive (POSIX `kill -0`). If alive, leave it untouched.
3. For each dead-pid entry:
   - Attempt `TeamDelete` against the recorded `team_id` if the current Claude Code session can address it (per-session limitation per Memory; usually it cannot).
   - Filesystem fallback: `rm -rf <team_dir>` for the recorded path.
   - Tear down the recorded `tmux_session` if it still exists (`tmux kill-session -t <session>` best-effort).
   - Remove the entry from `team_runs.json`.
4. Log the sweep results to the durable backend (`domain=log, subdomain=orphan-sweep`).

No schema change is required on the `projects` table. The artifact-file approach is intentional: it is local-machine state, not project state, and it survives backend-mode transitions cleanly.

## 10. Sub-agent team mode — specifics

### 10.1 Worker invocation

- Each worker = a fresh `Agent` tool invocation. No team, no persistent state between dispatches.
- Wave dispatch: PM emits an `Agent` tool call **per task in the wave**, all in a single message, with `run_in_background=true`.
- PM stays responsive while sub-agents run in background: PM reads bridge_db, responds to the human, monitors heartbeats.

### 10.2 Sub-agent bridge_db access

Sub-agents cannot use atelier's MCP tools directly (they are fresh invocations without atelier's MCP context). Following the kaizen-validated pattern:

- Atelier ships `scripts/bridge_send.py` and `scripts/bridge_read.py` — thin Python CLIs over the bridge_db SQLite file.
- Sub-agents invoke them via Bash. The briefing includes the helper-script paths and usage examples; the sub-agent's tool allowlist includes Bash with these scripts pre-approved.
- Heartbeat: every 60 s the sub-agent runs `python scripts/bridge_send.py --channel <worker_channel> --kind heartbeat`. The briefing carries the instruction.

### 10.3 Stall detection

- **Heartbeat** — every 60 s via bridge_db (worker-side timer).
- **Wall-clock cap** — 30 min per worker, configurable.
- **Soft kill** — heartbeat missed → PM emits an abort signal on the worker's bridge_db channel. Worker is expected to read the signal and exit cleanly.
- **Hard kill** — no response to soft kill within 60 s → PM hard-kills the process. Counts as a worker failure attempt against the 5-attempt budget.

### 10.4 User observability

- **Inline milestone updates.** PM emits proactive messages in its CC session: wave start, wave complete, escalation, finish-phase completion. No tmux required.
- **Status snapshot.** The user can ask `/atelier:status` for a snapshot of: current phase, current wave, per-worker state, recent escalations, open questions awaiting human input.

### 10.5 Parallel cap

- Default `MAX_PARALLEL_WORKERS=5`. Configurable via atelier config.
- If a wave has more tasks than the cap, PM batches: dispatches `MAX_PARALLEL_WORKERS` tasks, awaits any-complete, dispatches the next, etc. Wave is complete when all batched dispatches finish.

### 10.6 Worker output return value

Every sub-agent returns the canonical worker output envelope defined in §14.4. The envelope is mode-agnostic; the sub-agent's return-value channel is the only mode-specific detail (sub-agents return JSON as the `Agent` tool's result; agent-team workers post the JSON to their `worker-<task_id>-<attempt>` bridge_db channel).

## 11. Persona model

### 11.1 Existing roster

Atelier's `agents.db` (in Memex mode shared at `~/.memex/agents.db`; in local mode at `<project>/.ai/atelier.db`) already holds **61 roles** meeting the "best expert in field with very long experience" bar:

- PhDs from named top institutions.
- 18–31 years of named experience.
- Named credentials and publications.

Examples (non-exhaustive): `software-architect-1`, `backend-engineer-1`, `security-engineer-1`, `sdet-1`, `technical-writer-1`. The full roster is in `templates/agents/*.json`.

**No roster rework is needed for team mode.** The 61 existing personas are the supply.

### 11.2 Instantiation

- **PM-unilateral.** PM may instantiate any existing roster role at the team level (agent team mode) or per-task (sub-agent team mode) without human confirmation. This is orchestration, not modification.
- Selection is spec-driven: PM reads the 9-section spec and picks personas whose declared expertise matches the work.

### 11.3 Extension

Two extension paths require explicit human confirmation (because both produce permanent changes affecting future runs):

| Op | What changes | Confirmation |
|---|---|---|
| Register a new persona globally | New row in agents.db | Required |
| Update / refine an existing profile's content | Edit existing row in agents.db | Required |

PM proposes the change with a rationale. The human approves explicitly before atelier writes to agents.db. Both ops respect the v1.1.0 retrofit's bootstrap idempotency rules (§5 of [2026-05-16-atelier-memex-v2-retrofit-design.md](2026-05-16-atelier-memex-v2-retrofit-design.md)).

### 11.4 Reviewer independence

Reviewer / security / qa persona for a task MUST differ from the implementer persona for the same scope unit. Enforced twice:

1. Planner assigns reviewer persona ≠ implementer persona at task-list creation.
2. PM verifies at dispatch — if it ever finds reviewer_persona == implementer_persona, it refuses to dispatch and escalates (this is a planner bug, not a normal flow).

## 12. Git workflow

### 12.1 Branch and worktree shape

- `/atelier:run` creates a feature branch `atelier/<project-slug>` off `main` (or a configurable base — useful for staged multi-PR arcs; see Memory: "Kaizen base branch — lock LIFTED"). The exact config surface for "configurable base" (per-project setting vs per-run CLI flag vs machine setting) is deferred to the plan-writer — see §23.12.
- **One worktree per implementer in a wave.** Parallel implementers in the same wave each get an isolated git worktree, ensuring no two implementers fight over the same working tree.
- Worktrees are created under `<repo>/.atelier-worktrees/<task_id>/`.
- Workers push to their worktree's remote branch (`atelier/<project-slug>/<task_id>`).

### 12.2 Finish phase

- Finish worker merges all worktree branches into the feature branch in dependency-aware order.
- Finish worker opens a **single PR** for the feature branch against the base branch.
- PR title and body follow atelier's existing conventions (project description, task summary, links to durable-backend artifacts).

### 12.3 Worktree cleanup

- Worktrees with no uncommitted changes are auto-removed at finish-phase completion.
- Worktrees with uncommitted changes are **preserved** for inspection. The PR body includes a list of preserved worktrees with their state.

## 13. Resumability and abort flow

### 13.1 Resumability

At the start of every `/atelier:run`:

1. PM reads prior project history from the durable backend. This explicit "durable backend first" lookup is a direct application of the no-guessing rule (§5.3) at session boot.
2. If there is no in-progress project for the workspace, PM proceeds as a new run.
3. If there is in-progress state, PM asks the human: **"new or continuation?"**
   - **Continuation.** PM appends to the existing project row. The spec version is incremented if amended. Worktrees and bridge_db channels for incomplete tasks are reattached.
   - **New.** PM creates a fresh project row. The in-progress one is left intact (the user can resume it later).
4. **Aborted arcs are resumable.** PM detects an `aborted` project row and offers "resume from where you left off?" as a continuation option.

**Note on the β qualifier.** Earlier brainstorm notes carried a "(β)" qualifier on continuation. The locked decision as captured for this spec drops the qualifier — continuation is a first-class v1 feature, not behind a flag. If the implementation team finds continuation flakier than expected in fixture runs, gating it behind a config flag is a plan-writer decision (§23.13), not a design-time concession.

### 13.2 Abort triggers

Abort can be triggered by:

- Human typed at PM: "stop", "abort", or `/atelier:abort`.
- Failure-escalation choice menu — when PM escalates to human, "abort" is one option.
- System resource limits — out-of-memory, disk-full, or atelier-internal sanity checks.

### 13.3 Abort modes

| Mode | Trigger | Behavior |
|---|---|---|
| **Soft abort** | Default. `/atelier:abort` or "stop" at PM | Workers finish their current step, persist state, then exit. No new work dispatched. |
| **Hard abort** | `/atelier:abort --hard` | Immediate process termination. In-flight state may be lost. |

### 13.4 Cleanup on abort

- `TeamDelete` (agent team mode).
- Sub-agent termination (sub-agent team mode).
- Tmux panes torn down (agent team mode).
- Worktrees auto-cleaned if no uncommitted changes; preserved otherwise.
- Feature branch is **preserved on the remote** for inspection / resumption.
- Project row in durable backend marked `aborted`.
- **No PR created.**
- All durable-backend artifacts (spec, meeting minutes, partial task outputs) are preserved.
- An abort-report doc is written: `domain=postmortem, subdomain=abort` — captures phase reached, trigger, worker states, what was preserved.

## 14. Storage

### 14.1 bridge_db — transient signals

bridge_db is the channel-scoped signal bus. Stored in SQLite at `<project>/.ai/bridge.db` (gitignored). Schema is an extension of kaizen's point-to-point pattern to support multi-party channels.

**Channel naming (hierarchical):**

```
run-<project_id>:pm                          PM general inbox
run-<project_id>:worker-<task_id>-<attempt>  Per-worker channel (unique across retries)
run-<project_id>:team-meeting                Agent team mode meeting (team-wide visibility)
run-<project_id>:abort                       Abort broadcast (all workers monitor)
run-<project_id>:status                      PM-emitted milestone events
```

**Signal kinds.** `ping`, `question`, `answer`, `abort`, `heartbeat`, `milestone`, `side-query`. Each row carries `kind`, `from_agent`, `to_agent` (nullable for broadcasts), `channel`, `body`, `created_at`.

**Concurrency.** WAL mode mandatory (§15).

### 14.2 Durable backend — artifacts

Artifacts (anything that survives the run and is human-readable) live in the durable backend per the v1.1.0 retrofit:

- **Memex mode** — `~/.memex/atelier.db` + `~/.memex/index.db` via `memex:run`.
- **Local mode** — `<project>/.ai/atelier.db` via `internal/local/*` procedures.

Artifacts by domain:

| Artifact | Domain | Subdomain |
|---|---|---|
| Spec doc (9-section) | `project` | `spec` |
| Meeting minutes (agent team mode plan phase) | `meeting` | `plan` |
| Task list rows | n/a (`tasks` table) | per task |
| Field-analysis docs (sub-agent team mode plan phase) | `research` | `field-analysis` |
| Phase result docs (tdd, implement, review, security, qa, finish outputs) | `project_doc` | `<phase>-result` |
| Failure reports | `postmortem` | `failure` / `meeting-failure` / `abort` |
| Side-query logs (agent team mode) | `log` | `side-query` |
| PM milestone events | `log` | `milestone` |

### 14.3 Spec and task-list versioning

- **New doc per amendment.** Editing the spec or the task list produces a **new** durable-backend row, not an in-place UPDATE.
- `metadata.version = N` on the new row.
- `metadata.supersedes = <prior_doc_id>` on the new row.
- The latest version wins for new dispatches; the history is preserved (matches the v1.1.0 retrofit's `supersedes` relation convention — §6.9 of the retrofit spec).
- Workers read the latest version **at dispatch time**. Their output envelope records the `spec_version` and `task_list_version` they operated on. Later spec amendments do not retroactively invalidate in-flight worker output, but they DO trigger new dispatch waves (the planner produces an amended task list).

### 14.4 Worker output envelope (canonical — applies to ALL workers, both modes)

Every worker — sub-agent or agent-team member — emits the same JSON envelope when reporting done. Missing any field blocks "ping done." The envelope is the source-of-truth pointer; the durable backend holds the actual artifact content.

**Schema:**

```json
{
  "task_id": "<tasks.id>",
  "attempt": <int, 1-based>,
  "status": "done" | "abandoned" | "blocked",
  "output_doc_id": "<index_id of the primary output doc in the durable backend>",
  "open_questions_resolved": true,
  "rules_version": "<atelier-rules version this worker operated under>",
  "spec_version": <int>,
  "task_list_version": <int>,
  "spec_section_refs": ["acceptance-criteria-3", "scope-2"],
  "parent_worker_outputs": ["<index_id>", "..."]
}
```

**Field reference:**

| Field | Purpose |
|---|---|
| `task_id` | The `tasks.id` the worker was dispatched against |
| `attempt` | 1-based attempt counter; matches the suffix in the bridge_db channel name `worker-<task_id>-<attempt>`. Bumped by PM on every re-dispatch. Source of truth: `tasks.attempts` column (see §14.5). |
| `status` | Terminal state — see status enum below |
| `output_doc_id` | The `index_id` (Memex mode) or local doc id (local mode) of the worker's primary output artifact |
| `open_questions_resolved` | `true` iff the worker resolved every entry in its open-questions list before reporting done |
| `rules_version` | The `version` header value from `internal/team-mode-rules/SKILL.md` at dispatch time |
| `spec_version` | The spec doc version the worker read |
| `task_list_version` | The task list version the worker read |
| `spec_section_refs` | Which sections of the spec this work addresses (e.g. `["acceptance-criteria-2", "scope-3"]`) |
| `parent_worker_outputs` | Prior worker output `index_id`s this work builds on (e.g., an implement task references its tdd task's output) |

**`status` enum — transition semantics:**

| Value | Meaning | Transition rule |
|---|---|---|
| `done` | Self-verify passed (CI + checklist). Open questions all resolved. Output doc written. PM accepts and advances. | Terminal. |
| `blocked` | Worker cannot proceed but has not exhausted attempts. Open question awaiting PM/human, or external dependency not yet available. PM may re-dispatch after unblocking, bumping `attempt`. | Non-terminal; transitions to `done` or `abandoned` on next attempt. |
| `abandoned` | Worker exhausted its 5-attempt budget OR hit the wall-clock / token cap without converging. Failure report written. PM escalates to human; wave advances after human acknowledges. | Terminal. |

PM never sets `status` itself — workers self-report. PM validates against the dispatch context (task_id matches, attempt matches the dispatched attempt, etc.) before recording the envelope.

Workers populate the envelope as part of self-verify. The atelier-rules file (§16.2) carries the envelope-completeness check as a hard rule.

### 14.5 bridge_db schema sketch

The full schema specifics (index strategy, channel-table normalization choice) are deferred to the plan-writer (§23.1). The minimum shape required for this design to be implementable:

```sql
-- bridge_db at <project>/.ai/bridge.db
CREATE TABLE messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  channel      TEXT NOT NULL,           -- per §14.1 channel naming
  kind         TEXT NOT NULL,           -- ping|question|answer|abort|heartbeat|milestone|side-query
  from_agent   TEXT NOT NULL,           -- agent_id of sender (PM, worker, human)
  to_agent     TEXT,                    -- nullable for broadcasts (abort, milestone)
  body         TEXT NOT NULL,           -- message payload (JSON or plain)
  created_at   TEXT NOT NULL,           -- ISO 8601 with TZ
  read_at      TEXT                     -- ISO 8601; null = unread by intended consumer
);
CREATE INDEX idx_messages_channel_created ON messages(channel, created_at);
CREATE INDEX idx_messages_channel_unread  ON messages(channel, read_at) WHERE read_at IS NULL;
CREATE INDEX idx_messages_kind            ON messages(kind);
```

Channel naming is hierarchical (§14.1); whether to normalize channels into a separate `channels` table or keep denormalized on `messages` is a plan-writer call (§23.1). The denormalized shape above is the minimum viable schema. WAL mandatory per §15.

### 14.6 `attempt` tracking — where it lives

The 5-attempt budget (§5.2) and the `worker-<task_id>-<attempt>` channel name (§14.1) require a persistent attempt counter per task. It lives on the `tasks` table:

| Column | Type | Notes |
|---|---|---|
| `tasks.attempts` | INTEGER NOT NULL DEFAULT 0 | Incremented by PM on every dispatch (first dispatch → `attempts=1`). Source of truth for the envelope's `attempt` field. |
| `tasks.status` | TEXT NOT NULL | `pending`, `dispatched`, `done`, `blocked`, `abandoned`. Mirrors the envelope's `status` for terminal transitions; `pending` and `dispatched` are PM-managed pre-terminal states. |
| `tasks.last_attempt_at` | TEXT | ISO 8601 timestamp of the last dispatch. Used by stall detection. |

When PM exhausts the attempts budget for a task, it writes a failure report and transitions `tasks.status='abandoned'`. The wave advances.

## 15. Concurrency

- **WAL mode mandatory on bridge_db AND on the durable backend.** This is a hard invariant.
  - bridge_db (`<project>/.ai/bridge.db`): atelier opens the DB at session start and runs `PRAGMA journal_mode=WAL`. The pragma is verified by reading it back; if the result is not `wal`, atelier hard-fails with a setup error.
  - Local-mode durable backend (`<project>/.ai/atelier.db`): same pragma sequence at session start. Hard-fail if WAL cannot be enabled.
  - Memex-mode durable backend (`~/.memex/atelier.db`): atelier explicitly **asserts** WAL at session start by running `PRAGMA journal_mode` and verifying the result is `wal`. If WAL is not active, atelier attempts `PRAGMA journal_mode=WAL` on its own connection and re-verifies; on failure the run hard-fails with a clear message (Memex's store-management code is the upstream owner of journal mode for that DB; atelier does not silently tolerate non-WAL there because the locked invariant covers both backends, not just bridge_db). The session-start assertion is logged.
- **Single-writer-per-row pattern.** Each worker writes only its own rows; no two workers share a row. PM is the only writer for PM-owned rows (project, milestone events).
- **Short transactions only.** Parameter-bound writes; commit per row. No long-held read transactions.
- **Optimistic reads on snapshots taken at dispatch time.** A worker dispatched in wave N reads the spec / task list as it existed when dispatch started. Spec amendments emit new versions; in-flight workers continue with the snapshot they were dispatched against.

## 16. Reuse strategy

### 16.1 Phase procedures stay unchanged

The existing `internal/dev-*/SKILL.md` procedures (the dev-arc procedure set shipped by atelier) stay as-is, with one exception:

- **`internal/dev-design/SKILL.md`** is updated to use the 9-section spec template (§6.2). This is the only legacy procedure that changes shape; the others change only by virtue of being called through the new dispatch layer.

Team-mode behavior — wave dispatch, hard rules, output envelope — is layered in PM's dispatch briefing, NOT via conditionals in shared procedures.

### 16.2 Atelier-rules file

Lives at `internal/team-mode-rules/SKILL.md`. Versioned with a header:

```yaml
---
version: 1
---
```

Contains the hard rules from §5.3 (no silent deferrals, no guessing), the output envelope completeness check, and the self-verify protocol. PM's dispatch script reads this file at dispatch time and prepends it to every worker briefing. The worker output envelope records the `rules_version` it operated under, so atelier can later filter outputs by which rules era they came from.

### 16.3 Worker briefing composition

PM's dispatch script (`scripts/dispatch.py`) composes every worker briefing in this fixed order:

```
1. Atelier-rules header (versioned, from internal/team-mode-rules/SKILL.md)
2. Persona profile (loaded from agents.db by assigned_persona)
3. Phase procedure (internal/<phase>/SKILL.md verbatim)
4. Task-specific block:
   - task_id
   - parallel_group (wave)
   - dependencies (list of upstream task_ids)
   - parent_outputs (their index_ids)
   - description (from tasks row)
   - assigned_persona
5. Output requirements:
   - Required envelope fields (§14.4)
   - Phase-specific body schema (per dev-* SKILL.md)
6. Bridge_db wiring:
   - Channel ID for this worker
   - Helper script paths and usage (sub-agent mode)
   - Heartbeat instruction (60s cadence)
   - Abort-check instruction (poll the abort channel)
7. Self-verify protocol:
   - Project CI commands (read from project config)
   - Phase/persona checklist (read from phase + persona)
   - 5-attempt budget
   - Token/wall-clock cap
```

The composition is mode-agnostic. Sub-agent team mode receives this as the `prompt` argument to the `Agent` tool. Agent team mode receives this as the spawn-prompt at `TeamCreate` time and as per-task messages thereafter, relayed via Claude Code's `SendMessage` primitive (per Memory: "CC team-mode is async-only — spawn-prompt output is NOT auto-relayed; teammates must SendMessage back"). The dispatch script accordingly issues an explicit `SendMessage` per task in agent team mode rather than relying on the spawn-prompt to carry per-task content.

### 16.4 Persona profile loading

`scripts/dispatch.py` reads the persona profile from agents.db using the same path as the Atelier-Memex retrofit's `memex:core:get-agent` flow. Profiles are cached per-run to avoid re-reading on every dispatch.

### 16.5 `dev-design` and the PM phase — relationship

The PM phase (§6) is human-facing and dispatches no workers. PM walks the human through the 9-section template using a PM-side script (PM's own interview logic), and the resulting spec is stored at `domain=project, subdomain=spec`. PM does NOT invoke a worker against `internal/dev-design/SKILL.md` during the PM phase.

`internal/dev-design/SKILL.md` is the **worker-side** procedure used when a planner-assigned task is in the `design` domain — for example, an architecture-decision-record task or a sub-system-design task spun out of the plan phase as part of the task list. Such tasks dispatch workers normally per the common workflow, with the worker reading `dev-design/SKILL.md` as the phase procedure block in its briefing (§16.3 step 3).

What the v1.1.0-vs-team-mode change does to `dev-design` is **adopt the 9-section spec template as the output shape that worker-produced design docs must follow** — so design docs authored by workers downstream of the plan phase have the same skeleton as the PM-phase spec. The interview logic itself (the question-by-question grilling) lives only in PM's script, not in `dev-design/SKILL.md`.

In short:

| Surface | What runs it | What template it uses |
|---|---|---|
| PM phase spec authoring | PM (atelier-as-orchestrator) talking to human | 9-section template (§6.2) |
| Worker-produced design docs | A worker invoked via `internal/dev-design/SKILL.md` | 9-section template (the v1.1.0-vs-team-mode change) |

## 17. Memex / local mode compatibility

Team mode is **mode-agnostic** — both shapes (sub-agent and agent team) work in both backend modes (Memex and local), via the durable-backend abstraction from the v1.1.0 retrofit.

| Scenario | Behavior |
|---|---|
| First install, Memex absent | Silent fallback to local mode (matches v1.1.0 detection — §4.2 of the retrofit) |
| First install, Memex present but too old (< v2.2.0) | Bootstrap hard-fail with clear message (matches atelier#35 reframed). The `v2.2.0` floor is set in the v1.1.0 retrofit spec — see [2026-05-16-atelier-memex-v2-retrofit-design.md §6](2026-05-16-atelier-memex-v2-retrofit-design.md) "Prerequisite — Memex version floor." |
| Late Memex install (was local, now Memex available) | Existing migrate-prompt flow at next `/atelier:run` (matches v1.1.0 §8 of the retrofit) |
| Mode logged at session start | PM emits a milestone event recording which mode was active |

bridge_db is **always local** to the project (transient signals; no need for cross-machine sharing). Only the durable backend routes via mode detection.

## 18. Implementation roadmap (forward-looking primitives)

These primitives must exist before the plan phase of the team-mode implementation can run end-to-end. They are listed here so the plan-writer can scope them; this design intentionally does NOT enumerate per-wave tasks (that's the plan-writer's job).

| Primitive | Purpose | Source |
|---|---|---|
| `scripts/bridge_send.py`, `scripts/bridge_read.py` | Sub-agent bridge_db helpers | Port from kaizen |
| bridge_db schema | Multi-party channels (extension from kaizen's point-to-point) | New in atelier |
| `scripts/dispatch.py` | Composes briefings, tracks waves, monitors heartbeats | New in atelier |
| `internal/team-mode-rules/SKILL.md` | Versioned atelier-rules file | New in atelier |
| `internal/dispatch/templates/` | Briefing templates (one per phase + one common header) | New in atelier |
| `internal/dev-design/SKILL.md` update | Adopt 9-section spec template | Modification of existing |
| `scripts/sweep_leaked_teams.py` | Orphan-sweep for stranded teams | Port from kaizen |
| `scripts/abort.py` | Soft/hard abort handlers + cleanup | New in atelier |
| Worker output envelope validator | Used in self-verify | New in atelier |
| `/atelier:status` skill | Surface snapshot to user | New in atelier |
| `/atelier:abort` skill | Trigger abort flow | New in atelier |
| `tasks.parallel_group` consumer | Wave-based dispatch reads this column | Atelier#34 consumer (this design) |
| Tmux config writer | Writes `~/.config/atelier/tmux.conf` + `~/.tmux.conf` include | New in atelier |

## 19. Related issues

This design intersects with several open atelier issues. Land order and dependencies:

| Issue | Title | Relation to team mode |
|---|---|---|
| **atelier#34** | Reintroduce `tasks.parallel_group` | **CONSUMER NOW EXISTS** (this design). Land alongside the team-mode implementation — the schema column without a consumer is dead weight, and the consumer without the column cannot enforce wave dispatch. |
| **atelier#35** | Memex-too-old bootstrap hard-fail (reframed) | Independent fix. Not blocked by team mode; team mode inherits its behavior via the mode-detection flow. |
| **atelier#30** | Leaf `workspace_id` filter | Sub-item of #32; not team-mode-dependent. |
| **atelier#32** | §10 multi-workspace epic | Independent of team mode. Can land separately. Team mode operates on a single resolved workspace per run. |
| **atelier#33** | Cross-project task search in Memex mode | Independent. Team mode's task creation rides through the same `tasks` table; once #33 lands the search benefits flow through automatically. |

## 20. Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | Two execution shapes drift over time (sub-agent vs agent team mode behave differently for the same task) | The 7 dev phase procedures are shared; mode-specific logic is confined to dispatch + plan-phase shape. CI test asserts: same task definition + same persona → same output envelope schema across both modes. |
| 2 | Wave dispatch deadlocks (a task in wave N blocks but its retry pushes a dependency into wave N+2, etc.) | The 5-attempt cap + abandonment path prevents infinite blocks. Abandoned tasks are surfaced to the human explicitly. |
| 3 | bridge_db SQLite contention under heavy parallelism | WAL mode + single-writer-per-row + short transactions (§15). Default `MAX_PARALLEL_WORKERS=5` keeps the working set small. |
| 4 | Agent-team-mode tmux install corrupts a user's existing tmux setup | `~/.tmux.conf.bak.<iso8601>` backup written before any modification. Include line is one-line and idempotent. Consent prompt requires explicit user assent. |
| 5 | Orphan teams accumulate from crashed sessions | Per-run orphan sweep at `/atelier:run` start (§9.5) + filesystem fallback for cross-session orphans. |
| 6 | Worker silently guesses despite the no-guessing rule | The atelier-rules file ships the rule with examples; the output envelope requires `open_questions_resolved=true` and ALL assumptions to be labeled in the output doc. CI on a fixture run asserts no `[assumption — N% confidence]` is missing the label. |
| 7 | Planner assigns reviewer = implementer persona | Double enforcement: planner-time + PM-dispatch-time. Either layer can reject. Planner-bug counted as failure attempt. |
| 8 | 9-section spec phase drags on indefinitely | Specificity bar is concrete (each section has measurable criteria, §6.2). PM may refuse a premature "done" but cannot indefinitely refuse the human. Human always has the option to escalate by accepting lower specificity in writing (recorded as a Risk in section 8). |
| 9 | Meeting in agent team mode spirals | Wall-clock + message-count backstops (§7.2). On backstop the planner is forced to declare done with whatever state was reached. |
| 10 | Sub-agent team mode loses context between waves (one-shot Agent calls) | The durable backend IS the context. Worker output envelopes carry `parent_worker_outputs`; the next-wave briefing includes parent_outputs. The lack of in-process memory is by design — durability is the persistence model. |
| 11 | `parallel_group` set wrong by planner (overly serial or unsafe parallelism) | Planner owns wave assignment (locked decision §5.4); planner briefing includes wave-design guidance derived from the dependency graph. Wave-validity heuristics (over-serialization detection, file-touch conflict detection) are deliberately out of scope for v1 — the planner-owned assignment plus reviewer/human inspection of the task list is the v1 mitigation. If empirical drift shows up, a heuristic layer can be added in a follow-on spec. |
| 12 | Worker output envelope fields are forged (worker self-reports done with bogus refs) | Self-verify includes envelope validation against the dispatched task_id + spec snapshot. The phase reviewer (review task) re-validates parent_worker_outputs by reading them. End-of-run finish phase audits the chain. |

## 21. Testing strategy

Team mode introduces enough new surface that a dedicated testing strategy is required. Tests fall into the categories below. Concrete acceptance criteria per test live in the plan, not here; this section defines the categories and the invariants each category must defend.

### 21.1 Wave dispatch tests

- **Strict ordering.** Given a task list with waves `[0,0,1,1,2]`, the dispatcher must dispatch wave 0 tasks concurrently, wait for both, then wave 1, etc. Asserted by spying on the dispatch order.
- **Wave does not start early.** A task in wave N+1 must not be dispatched while any wave N task is non-terminal (`pending`, `dispatched`, or `blocked`). Negative test: inject a blocked wave-N task and assert no wave-N+1 dispatch.
- **Wave advance on abandonment.** A wave-N task that reaches `abandoned` does NOT block the wave; the next wave proceeds (with human acknowledgement recorded).
- **Parallel cap honored.** With `MAX_PARALLEL_WORKERS=2` and 5 tasks in a single wave, the dispatcher batches 2-at-a-time until all 5 complete; wave is not "complete" until all 5 are terminal.
- **`parallel_group` null rejected.** Planner output with a null `parallel_group` is rejected at task-list-creation time.

### 21.2 Envelope validation tests

- **Missing field blocks done.** Submit an envelope missing any of the §14.4 required fields; assert PM rejects "ping done" and the worker is re-prompted to complete the envelope.
- **`status` enum validity.** Submit a status outside `{done, blocked, abandoned}`; assert rejection.
- **`attempt` matches dispatch.** Submit envelope with `attempt=2` when PM dispatched `attempt=1`; assert rejection.
- **`rules_version` matches atelier-rules at dispatch.** Workers can't claim a different rules era than what was prepended to their briefing.
- **`spec_version` and `task_list_version` are snapshot values at dispatch.** Negative test: spec is amended mid-run; in-flight worker envelope still records the snapshot version.

### 21.3 Reviewer-independence enforcement tests

- **Planner-time rejection.** Task list with `review` task `assigned_persona == implement` task `assigned_persona` (same task pair) is rejected at task-list-creation time.
- **PM-dispatch-time rejection.** Forcibly inject the same-persona violation past the planner check; PM dispatch refuses and escalates.
- **Reviewer dispatched separately.** Reviewer briefing does NOT include the implementer's working memory; only the published output doc + spec.

### 21.4 Briefing composition tests

- **Fixed order.** §16.3's seven-block order is asserted by snapshot test on a synthetic worker dispatch.
- **All blocks present.** No block is silently omitted. A worker dispatched without the atelier-rules header MUST fail self-verify (rules_version cannot be set).
- **Persona profile loaded.** Asserted by verifying the briefing contains the named persona's biographical block.
- **Mode-agnostic composition.** Same task definition produces byte-identical briefings up to mode-specific bridge_db wiring (sub-agent gets bash-helper paths; agent-team gets channel addresses).

### 21.5 Orphan sweep tests

- **Dead-pid entry cleaned.** Pre-populate `~/.atelier/team_runs.json` with a fake entry whose pid is not running; assert the sweep removes the entry, attempts filesystem cleanup, and logs to durable backend.
- **Live-pid entry preserved.** Pre-populate with an entry whose pid IS running (use the test runner's own pid); assert the sweep leaves it alone.
- **Idempotent.** Sweep run twice in a row produces no errors; second run is a no-op on an empty `team_runs.json`.

### 21.6 Tmux install idempotency tests

- **First run.** No prior `~/.tmux.conf` include line → atelier writes the include + backup + atelier config; consent prompt fires.
- **Second run.** Include line already present → atelier detects it; no backup written; no consent prompt; no `~/.tmux.conf` modification.
- **Backup is correct.** The `~/.tmux.conf.bak.<iso8601>` file is byte-identical to the pre-modification `~/.tmux.conf`.
- **Atelier-owned config writable.** `~/.config/atelier/tmux.conf` is created with the 3 keybindings (§9.3) and nothing else.

### 21.7 Abort recovery tests

- **Soft abort.** `/atelier:abort` mid-wave → workers reach a stable step, persist, exit; project row marked `aborted`; no PR created; feature branch preserved on remote; abort-report written.
- **Hard abort.** `/atelier:abort --hard` → process termination; project row still marked `aborted` via cleanup-on-startup recovery; partial state may be lost (asserted as expected).
- **TeamDelete on abort (agent team mode).** Team is deleted; `~/.atelier/team_runs.json` entry removed; tmux session torn down.
- **Worktree state preserved if dirty.** Dirty worktree → preserved; clean worktree → removed.

### 21.8 Resume tests

- **Fresh start.** No prior project row for the workspace → PM proceeds as new run.
- **Continuation.** Prior in-progress project → PM prompts; on "continuation" PM reattaches incomplete tasks' bridge_db channels and worktrees; spec is reloaded at its latest version.
- **Aborted resume.** Aborted project row → PM offers resume; on accept, project resumes from the last terminal wave.
- **Spec amendment mid-resume.** User amends spec on resume → new spec version row; in-flight tasks re-dispatched against the new version after the current wave drains.

### 21.9 WAL invariant tests

- **bridge_db is WAL at session start.** Test pragma value matches `wal`; hard-fail if not.
- **Local-mode durable backend is WAL.** Same assertion against `<project>/.ai/atelier.db`.
- **Memex-mode durable backend is WAL.** Atelier's WAL assertion fires against `~/.memex/atelier.db`; test mocks a non-WAL state and asserts atelier hard-fails the run with a clear message.

### 21.10 Hard-rules enforcement tests (fixture run)

- **No silent deferral.** A worker emits `open_questions_resolved=true` while its output doc has an unresolved entry on the open-questions list → PM rejects.
- **No silent guess.** Worker output doc contains an unlabeled inference (heuristic test: regex for "I'll assume" without an adjacent `[assumption — N% confidence]` label) → flagged in CI.

## 22. Out of scope (do not implement without spec revision)

- Multi-machine team coordination.
- Multi-human teams.
- A "no team" / direct execution mode.
- Mid-run mode switching (sub-agent ↔ agent team).
- Cross-run team reuse / hot teams.
- Persisting agent-team-mode panes across human tmux sessions.
- Mid-phase persona swap without going through failure-report dispatch.
- Automatic resumption without human prompt.
- Worker-to-worker direct messaging that bypasses PM (workers ↔ workers via bridge_db is allowed only via team-meeting channel in agent team mode plan phase).
- Cross-project worker dispatch (a worker for project A cannot be dispatched from a `/atelier:run` for project B).

## 23. Open decisions deferred to `writing-plans`

These do not block design approval but are decisions the plan-writer will need to make:

1. **bridge_db schema specifics — beyond §14.5 sketch.** Whether to normalize channels into their own `channels` table (with FK from `messages`) or keep the denormalized `messages.channel` column. Final index strategy for hot-path reads (per-worker channel + abort broadcast + unread filter). The §14.5 schema is the minimum-viable baseline.
2. **`MAX_PARALLEL_WORKERS` config location.** Per-project (in atelier.db `projects.config`) vs per-run (CLI flag on `/atelier:run`) vs both.
3. **Briefing template format.** Plain markdown vs Jinja vs Python f-string composition. Trade-off: plain markdown is auditable in version control; templating gives composition power.
4. **`/atelier:status` snapshot detail.** Minimal (phase + wave) vs verbose (every worker's last heartbeat + open questions). Defer to user feedback after v1.
5. **Tmux pane layout tile algorithm.** Fixed grid vs golden-ratio vs aspect-aware. Pick whatever tmux's split command makes easiest.
6. **Heartbeat cadence verification.** Defaults stated in §10.3 (60 s heartbeat, 30 min wall clock) are design-time decisions. The plan-writer should pick stress-test thresholds for the fixture run that exercise the soft-kill / hard-kill paths. (The defaults themselves are not deferred — they are the locked values.)
7. **How the entry prompt is rendered.** Pure-text Y/N vs a short menu. Defer to UX taste.
8. **Side-query log retention.** Always keep in durable backend vs trim after run completes. Keep by default (durable backend is cheap).
9. **Failure-report richness.** Minimal (task_id + last error) vs rich (full transcript + envelope chain). Default to rich; failure reports are where future-run learning lives.
10. **Atelier-rules versioning bumps.** SemVer vs monotonic integer. Lean monotonic (simpler; the rules don't have a public-API surface that needs SemVer signaling).
11. **Concrete token cap per attempt.** Wall-clock cap is locked at 30 min default; token cap is deferred. Options: a hard atelier-side budget (e.g. 200k tokens per attempt) tracked via the dispatching API's usage telemetry; or relying on the underlying `Agent`/team-member tool's own context-window limit. Plan-writer picks and documents.
12. **Configurable base for the feature branch (§12.1).** Currently spec'd as "off `main` or a configurable base." The config surface (per-project, per-run flag, or per-machine setting) is a plan-writer decision.
13. **Continuation behind a config flag (§13.1).** If continuation proves flakier than expected in fixture runs, gating it behind a `--allow-continuation` flag is a plan-writer call.

# Atelier — Auto-Trigger and Soft Walls — Design Spec

**Date:** 2026-05-14
**Status:** Draft (Section 1 of N)
**Author:** nitekeeper + Claude (brainstorming session)

---

## 1. Goal and scope

### Problem

Atelier today is invocation-driven. The agent does not automatically engage Atelier on a new-work request — the user (or the target project's `CLAUDE.md`) has to explicitly route through `project:create` and `dev:design`. Compared to Superpowers' `using-superpowers` SessionStart bootstrap and `brainstorming` auto-trigger, the contrast is sharp: Superpowers asks the agent to invoke its methodology on any creative work; Atelier sits silent until called.

The other half of the problem: the phase state machine in `migrations/003_phases.sql` enforces strict ordering through hard walls. Five skills (`dev:plan`, `dev:tdd`, `dev:review`, `dev:security`, `dev:qa`) cannot be invoked unless the previous phase has been formally `approved`. This worked for the original waterfall framing, but it breaks down when real work needs out-of-phase moves — spikes during design, security review on a subsystem before whole-project review approval, plan revisions after TDD reveals an invalid assumption.

### Goal

Make Atelier auto-engage on new-work requests (with explicit user confirmation), and soften the phase walls so skills can be invoked when they're needed rather than only when the gate has been satisfied — without losing Atelier's identity as a methodology rather than a CRUD toolkit.

### In scope

- A bootstrap skill (`using-atelier`) acting as the canonical methodology source
- Four mechanisms that surface the bootstrap content: frontmatter (narrowed), SessionStart hook, `session_open.py` extension, CLAUDE.md template snippet
- A three-option ask gate ("full arc / diagnose / direct") that the agent uses when it detects new-work
- A detection heuristic for "new-work" with explicit Red Flags for over-firing
- Conversion of the five phase walls from blocking to warn-confirm-log
- A bypass log so out-of-phase invocations remain auditable for retros
- Decoupling skill invocation from phase advancement (skills can be invoked freely; phases advance only on explicit `workflow.py advance`)
- Simplification of `dev:diagnose` to remove its `allow_from_any` special-case status (no longer needed)

### Out of scope

- Wave-based parallelism / multi-story execution model (this would be a separate, much larger design — the agentic-agile manifesto source can inform it later)
- Removing phases entirely (phases remain as state tracking)
- Reworking which phases exist or what they mean (the 19 phases stay)
- Changes to non-dev skills (CRUD, workspace, session lifecycle) beyond minimal frontmatter additions
- Cross-project state (single project per workspace remains the model)

### Success criteria

- A first-time user can open Claude in a repo with Atelier installed, say "I want to add X", and the agent will offer the three routings without any user-side setup other than running the Atelier install
- A user mid-arc at `tdd:green` can invoke `dev:design` to revise the design and the system logs the bypass, advancing back to `design:open` only on explicit advancement
- The `using-atelier` skill is the single editable source of methodology; changes to it propagate via the four surfaces without separate edits
- Existing tests in atelier remain passing; no regression in the phase state machine's tracking behavior (only its enforcement behavior)

---

## 2. Architecture: single source, four surfaces

### 2.1 Canonical methodology source — `skills/using-atelier/SKILL.md`

A single file holds the entire Atelier methodology. All four surface mechanisms derive from it. When methodology changes, this file changes; the other mechanisms either reference it, summarize it, or are regenerated from it. This prevents the four surfaces from silently disagreeing as the methodology evolves.

The file has five structured sections that downstream mechanisms can parse:

1. **Trigger contract** — the rule the agent applies on every user message
2. **Red Flags** — detection-pattern table for identifying new-work requests
3. **Phase guidance** — per-phase recommended next actions (consumed by `session_open.py`)
4. **Dev arc reference** — the canonical phase ordering and skill mapping
5. **Bypass procedure** — the shared pattern every dev skill references when handling a soft-wall mismatch

### 2.2 Trigger contract — what the agent does

The contract is phrased as a procedural rule the agent follows before responding:

> **On every user message, before responding:**
>
> 1. If a project is active and its phase is not `handoff:complete`, continue the current arc — do NOT ask. Proceed with the phase-recommended skill or with the user's explicit request.
> 2. If the message is a question, exploration, read-only request, or trivial edit (see Red Flags below), handle directly without asking.
> 3. If the message describes new development work, ask the user one of three routings:
>    - **(a) Full Atelier arc** — invoke `project:create`, then `dev:design`. Routes through design → plan → tdd → review → security → qa → handoff with soft walls.
>    - **(b) Bug fix** — invoke `dev:diagnose` against the active project. Captures pre-diagnose phase, writes regression test, restores on resolution.
>    - **(c) Handle directly** — do the work without Atelier orchestration. No project created, no phase tracking.
>
> The agent waits for an explicit response before acting. Default to option (a) if the user says "yes" without specifying.

### 2.3 Detection — Red Flags

The agent must judge "new development work" reliably. Encoded as a Red Flags table inside `using-atelier/SKILL.md`:

| Thought | Reality |
|---|---|
| "User just wants a quick fix" | Quick fixes still go through option (b). Ask. |
| "This is too small to need design" | Ask. User can pick option (c). |
| "User is asking a question, no need to ask" | Correct — questions don't fire. Only work requests fire. |
| "Project is already active, no need to ask" | Correct — don't re-ask mid-arc. Continue current phase. |
| "User said 'how do I X' so it's a question" | Verify: are they asking how to do something, or asking the agent to do something? Latter fires. |
| "User said 'rename X to Y' — it's a tiny edit" | Tiny mechanical edits do not fire. Substantive renames (refactors affecting >5 files) fire. |
| "Refactor isn't new work" | Substantive refactors are new work. They get specs and reviews. Ask. |

**Firing patterns (examples):**
- "I want to add X" → fires
- "Build a system that does Y" → fires
- "The bug in Z is back" → fires (option b is the recommended route)
- "Refactor the auth module" → fires
- "How does this codebase handle X?" → does not fire (question)
- "Show me the file at path Y" → does not fire (read-only)
- "Fix the typo on line 42" → does not fire (trivial edit)
- "List the open tasks" → does not fire (CRUD)

### 2.4 Phase guidance — what to recommend mid-arc

A structured table inside `using-atelier/SKILL.md`. Consumed by `session_open.py` to append phase-specific guidance after its phase announcement.

| Phase | Recommended next action | Skill |
|---|---|---|
| `design:open` | Continue grilling. Do not write code yet. | `dev:design` |
| `design:approved` | Draft the implementation plan. | `dev:plan` |
| `plan:open` | Continue refining the plan with the user. | `dev:plan` |
| `plan:approved` | Write the first failing test. | `dev:tdd-red` |
| `tdd:red` | Write minimal implementation to make tests pass. | `dev:tdd-green` |
| `tdd:green` | Refactor with tests still passing. | `dev:tdd-refactor` |
| `tdd:clean` | Either continue TDD (new test) or advance to review. | `dev:tdd-red` or `dev:review` |
| `review:open` | Address findings or mark as approved. | `dev:review` |
| `review:changes-requested` | Apply requested changes, then re-review. | `dev:review` |
| `review:approved` | Run security review. | `dev:security` |
| `security:open` | Apply security findings or mark approved. | `dev:security` |
| `security:approved` | Run QA review. | `dev:qa` |
| `qa:open` | Address QA findings or mark approved. | `dev:qa` |
| `qa:approved` | Close out the project. | `dev:handoff` |
| `diagnose:open` | Reproduce the bug, write regression test, fix root cause. | `dev:diagnose` |
| `diagnose:resolved` | Restore to pre-diagnose phase. | `dev:diagnose` (final steps) |
| `handoff:complete` | Project is closed. New work requires a new project. | — |

### 2.5 Dev arc reference

A compact section restating the canonical flow:

```
design → plan → tdd (red ⇄ green ⇄ clean) → review → security → qa → handoff
              ↑                                                            
              ╰─ diagnose (entered from any non-terminal phase, restored on resolve)
```

This section is the human-readable backup of the phase guidance table.

### 2.6 The four surfaces

| # | Surface | What it carries | Source |
|---|---|---|---|
| 1 | Frontmatter on 4 SKILL.md files | Per-skill `description: Use when…` triggers for `using-atelier`, `ingest`, `save`, `load` | Hand-authored per skill |
| 2 | SessionStart hook (`hooks/session_start.py`) | Full body of `using-atelier/SKILL.md` injected as system context every session | Reads from canonical file |
| 3 | `session_open.py` extension (existing PreToolUse hook) | Phase-aware guidance appended after the existing phase announcement | Parses phase guidance table from canonical file |
| 4 | CLAUDE.md template snippet (`templates/CLAUDE-snippet.md`) | Short backup methodology + pointer to canonical file | Hand-maintained summary |

**Mechanism interaction:**
- Surface 2 is primary. With the SessionStart hook installed, the agent has the full trigger contract in context from the first user message.
- Surface 3 refines once a project exists (phase-specific guidance).
- Surface 4 is the belt-and-suspenders fallback if the hook isn't installed or runs after the first message.
- Surface 1 covers session-lifecycle skills that genuinely benefit from `Use when…` description routing.

No surface duplicates the canonical content verbatim except surface 2 (which is regenerated by the hook on every load — by design, since it IS the canonical content).

---

## 3. Soft walls

### 3.1 The change

Today, `workflow.py:check_gate(project_id, skill)` raises `WorkflowError` when the project is not in the required phase, and the calling skill aborts. Under soft walls, `check_gate` no longer raises — it returns a structured result that the caller (a skill or an interactive prompt) acts on. Out-of-phase invocations become *possible but logged*, not impossible.

| Today | Proposed |
|---|---|
| `check_gate` raises `WorkflowError` on mismatch | `check_gate` returns `{allowed, current_phase, required_phase, bypass_logged}` |
| Skill aborts on `WorkflowError` | Skill asks user to confirm the bypass, then proceeds with `bypass_logged=True` |
| No record of intent to bypass | New `phase_bypasses` table records every bypass |
| User has no override | User confirms each bypass interactively, or passes `--force` to skip the confirmation |
| Phase advances on successful gate-satisfied skill invocation | Phase advancement is decoupled — invocation no longer advances phase; only explicit `workflow.py advance` does |

### 3.2 `workflow.py:check_gate` — new signature

```python
def check_gate(db_path: str, project_id: int, skill: str) -> GateResult:
    """Check whether `skill` is in-phase for `project_id`.

    Returns GateResult with:
      - allowed: bool — True if phase satisfies the gate, OR if no gate exists
      - current_phase: str
      - required_phase: str | None — None if no gate
      - reason: str — human-readable explanation for the caller to display
    Does NOT raise on mismatch. Callers decide whether to proceed.
    """
```

Skills no longer abort on a gate miss. Instead, the skill procedure becomes:

1. Call `check_gate`.
2. If `allowed`, proceed normally.
3. If not `allowed`:
   - Display: "Project is at `<current>`, this skill normally requires `<required>`. Proceed anyway? (yes/no)"
   - On `yes`: call `workflow.py log-bypass <project_id> <skill> <current_phase> <required_phase>` to write a record to `phase_bypasses`, then proceed.
   - On `no`: stop. Tell the user: "Advance the project to `<required>` first, or pick a different skill."

### 3.3 New table: `phase_bypasses`

```sql
CREATE TABLE phase_bypasses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    skill           TEXT NOT NULL,
    current_phase   TEXT NOT NULL,
    required_phase  TEXT NOT NULL,
    bypassed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    agent_id        TEXT REFERENCES agents(id),
    note            TEXT
);

CREATE INDEX phase_bypasses_project_idx ON phase_bypasses(project_id);
```

- `agent_id` allows attributing bypasses to the agent that performed them (useful in multi-agent sessions).
- `note` is optional free text the user can attach when bypassing.
- `dev:handoff` retro logic queries this table to surface bypass patterns ("we bypassed plan→tdd 8 times — why?").

Migration: a new file `migrations/005_soft_walls.sql` creates the table. The `phases` and `phase_transitions` tables are unchanged. The `skill_gates` table is also unchanged — its rows still record the *recommended* phase per skill; they just stop being enforced as hard walls.

### 3.4 New script command: `workflow.py log-bypass`

```
python atelier/scripts/workflow.py log-bypass <project_id> <skill> <current_phase> <required_phase> [--agent <agent_id>] [--note "<text>"]
```

Writes one row to `phase_bypasses`. Idempotent on repeated identical calls within the same minute (avoids double-logging if a skill retries).

### 3.5 Phase advancement decoupling

Today, skill invocation and phase advancement are intertwined in skill procedures. For example, `dev:design` step 7 is `workflow.py advance <project_id> design:approved`. With soft walls, this stays the same — the skill explicitly advances when its work is genuinely done.

What changes: invocation of a skill is no longer implicit advancement, and a successful gate check does not trigger any state change. Phase advancement happens *only* through explicit `workflow.py advance` calls inside skill procedures. This means:

- Invoking `dev:plan` while at `design:open` (with bypass confirmed) does NOT advance to `plan:open`.
- The project stays at `design:open`. The plan-work happens, but the canonical phase has not moved.
- If the user later wants to make the plan-work official, they invoke `dev:design` step 7 (advance to `design:approved`) and then `dev:plan` step N (advance to `plan:approved`).

This separation makes phases honest about what they track: **canonical, approved state**, not "any work that touched this layer".

### 3.6 `dev:diagnose` simplification

Today, `dev:diagnose` is special because `diagnose:open` has `allow_from_any = 1` — it's the only escape hatch. With soft walls, every skill effectively has allow-from-any (because gates don't block). So:

- The `allow_from_any` flag on `diagnose:open` is no longer load-bearing for invocation. It can stay (harmless) or be removed in a future migration.
- The `dev:diagnose` skill itself keeps its current procedure unchanged: capture pre-diagnose phase, reproduce, write regression test first, fix root cause, restore.
- The `phase_bypasses` table is *not* used for diagnose entries — diagnose has its own session-record discipline (`pre_diagnose_phase` field) which is more structured than a generic bypass. Diagnose is a structured workflow that happens to bypass; not all bypasses are diagnose.

### 3.7 What stays the same

- The 19 phases in the `phases` table — unchanged.
- The `phase_transitions` table — unchanged. Transitions are still validated on `advance`.
- `workflow.py advance` — unchanged. Still enforces valid transition graph.
- `workflow.py force-phase` — unchanged. Still the override for transitions outside the graph (e.g., diagnose restoration).
- `skill_gates` table — unchanged in structure, but rows are read by `check_gate` as recommendations rather than walls.

### 3.8 Skill procedure updates

Every skill currently containing `workflow.py check-gate ... or stop` becomes:

```
python atelier/scripts/workflow.py check-gate <project_id> <skill>
# If allowed: proceed.
# If not allowed and the output says "bypass available":
#   1. Confirm with user: "Project is at <current>, this skill normally requires <required>. Proceed?"
#   2. On yes: python atelier/scripts/workflow.py log-bypass <project_id> <skill> <current_phase> <required_phase>
#   3. Continue procedure.
```

This pattern lives in `using-atelier/SKILL.md` as a dedicated "Bypass procedure" section, so each dev skill can reference it rather than re-document it. The canonical file thus contains five structured sections: trigger contract, Red Flags, phase guidance table, dev arc reference, and bypass procedure.

---

## 4. Deliverables — file-level

### 4.1 New files

| Path | Purpose |
|---|---|
| `skills/using-atelier/SKILL.md` | Canonical methodology source. Contains frontmatter, trigger contract (§2.2), Red Flags table (§2.3), phase guidance table (§2.4), dev arc reference (§2.5), and the bypass procedure pattern (§3.8). |
| `hooks/session_start.py` | New SessionStart hook. Reads `skills/using-atelier/SKILL.md` and prints its body to stdout as system context. Outputs nothing on error (Option B from existing hook spec — never blocks). |
| `templates/CLAUDE-snippet.md` | Short backup methodology + pointer to `using-atelier`. Consumer pastes into target project's `CLAUDE.md`. ~30 lines. |
| `migrations/005_soft_walls.sql` | Creates `phase_bypasses` table and index. Idempotent (`CREATE TABLE IF NOT EXISTS`). |
| `tests/test_using_atelier_skill.py` | Validates `using-atelier/SKILL.md` parses correctly — required sections present, phase guidance table machine-readable, frontmatter valid YAML. |
| `tests/test_session_start_hook.py` | Tests `session_start.py` outputs the using-atelier body, handles missing file gracefully, exits 0 even on errors. |
| `tests/test_phase_bypasses.py` | Tests the `phase_bypasses` table: rows insert correctly, `log-bypass` command writes the expected fields, idempotency within one minute. |
| `tests/test_soft_walls.py` | Tests `check_gate` returns `GateResult` (not raises), tests bypass flow end-to-end at the script level. |

### 4.2 Modified files

| Path | Change |
|---|---|
| `skills/ingest/SKILL.md` | Add YAML frontmatter with `description: Use when starting a new session and the agent needs to load prior session state and active project context`. |
| `skills/save/SKILL.md` | Add YAML frontmatter with `description: Use when ending a session or at a meaningful checkpoint — captures session state for the next resume`. |
| `skills/load/SKILL.md` | Add YAML frontmatter with `description: Use when resuming work mid-arc and needing the latest session context loaded into the conversation`. |
| `scripts/workflow.py` | Rewrite `check_gate` to return `GateResult` instead of raising. Add new `log-bypass` subcommand. Keep all other behavior unchanged. |
| `hooks/session_open.py` | Extend `format_output` (or equivalent) to append phase-specific guidance after the phase announcement. Read guidance from `using-atelier/SKILL.md` phase guidance table. Fall back gracefully if file is missing. |
| `skills/dev-design/SKILL.md` | Step 1 updated to use new `check_gate` return value. Step 7's `advance` unchanged. |
| `skills/dev-plan/SKILL.md` | Step 1 updated to handle bypass flow (confirm with user on mismatch, call `log-bypass`). |
| `skills/dev-tdd-red/SKILL.md` | Same as above. (Note: there are three TDD skills — red, green, refactor — each gets the bypass pattern.) |
| `skills/dev-tdd-green/SKILL.md` | Same pattern. |
| `skills/dev-tdd-refactor/SKILL.md` | Same pattern. |
| `skills/dev-code-review/SKILL.md` | Same pattern. |
| `skills/dev-security-review/SKILL.md` | Same pattern. |
| `skills/dev-qa-review/SKILL.md` | Same pattern. |
| `skills/dev-diagnose/SKILL.md` | Step 1 updated to use new `check_gate` (always allowed — no gate). Procedure otherwise unchanged. |
| `skills/dev-handoff/SKILL.md` | Step 1 updated. New addition: handoff queries `phase_bypasses` to surface bypass patterns in the retro summary. |
| `README.md` | Add a section: "Auto-trigger contract" — pointer to `using-atelier` + brief description. Update Setup section to mention installing the SessionStart hook. |
| `CLAUDE.md` (in atelier repo) | Add a section explaining the canonical-source-plus-four-surfaces architecture so future maintainers understand why the design is structured this way. |
| `CHANGELOG.md` | Add v0.2.0 entry. |

### 4.3 Unchanged files (explicitly)

- `migrations/001_initial_schema.sql` through `migrations/004_tasks_parallel.sql` — unchanged.
- `migrations/003_phases.sql` — the seed data for `phases`, `phase_transitions`, and `skill_gates` is unchanged. Only the *interpretation* of `skill_gates` changes (in `workflow.py`).
- `scripts/db.py`, `scripts/projects.py`, `scripts/tasks.py`, `scripts/agents.py`, `scripts/roles.py`, `scripts/documents.py`, `scripts/meetings.py`, `scripts/workspace.py`, `scripts/session.py` — no changes.
- All CRUD and workspace `SKILL.md` files — unchanged (no frontmatter added per Section 2.6).

### 4.4 File-count summary

- 8 new files
- 18 modified files (3 session-skill frontmatter, 1 workflow.py, 1 session_open.py, 10 dev-skill bypass updates, README, CLAUDE.md, CHANGELOG)
- ~14 files explicitly unchanged

The change is broad (every dev skill touched) but shallow per file (Step 1 of each dev skill gets the bypass pattern; other procedures untouched).

---

## 5. Testing strategy and open questions

### 5.1 Test coverage targets

| Layer | What to test | How |
|---|---|---|
| `using-atelier/SKILL.md` parseability | Frontmatter parses, required sections present, phase guidance table machine-readable, Red Flags table parses | `tests/test_using_atelier_skill.py` — load the file, parse YAML frontmatter, regex/scan for the named sections, assert all 17 phase rows are present. |
| `check_gate` return contract | Returns `GateResult` (never raises), `allowed` matches the phase-vs-skill_gates relationship, `reason` non-empty | `tests/test_soft_walls.py` — set up projects in various phases, call `check_gate` for each (skill, phase) combination, assert structured return. |
| `log-bypass` subcommand | Writes correct row, idempotent within 1-minute window, FK constraints honored | `tests/test_phase_bypasses.py` — run the script, query the table, assert fields. Run twice within one minute; assert one row. |
| `dev:handoff` bypass surfacing | Handoff retro queries `phase_bypasses` and includes pattern summary | `tests/test_handoff_with_bypasses.py` (new) or extend existing handoff test — seed bypasses, run handoff, assert output contains bypass count. |
| `session_start.py` hook | Outputs canonical content, never errors out fatally, exits 0 on missing file | `tests/test_session_start_hook.py` — run hook with present/missing canonical file, assert stdout + exit code. |
| `session_open.py` extension | Phase-specific guidance appended correctly for every phase, falls back gracefully | Extend `tests/test_session_open_hook.py` — assert each phase produces the expected guidance line. |
| Dev skills with bypass | Each updated skill respects the bypass flow at step 1 | Unit test per skill is overkill; one integration test exercises the pattern (`tests/test_skill_bypass_flow.py`). |
| Migration 005 | `phase_bypasses` table created, indexes present, idempotent | `tests/test_migrations.py` — extend existing migration test to apply 005 on fresh DB and on already-migrated DB. |

Aggregate target: existing 189 tests pass + ~25 new tests across the new test files = ~214 tests total. No regression in the existing suite.

### 5.2 Manual verification

Spec implementation is not complete until these manual scenarios all behave correctly:

1. **Cold install:** new repo, no Atelier setup, run the install steps from README; verify the SessionStart hook fires, `using-atelier` body appears in session context, agent offers the three-routing ask on a new-work message.
2. **Three-routing happy path (option a):** user says "I want to build X"; agent asks; user picks (a); `project:create` + `dev:design` invoked; project lands at `design:open`.
3. **Three-routing diagnose path (option b):** existing project mid-arc; user reports a bug; agent asks; user picks (b); `dev:diagnose` runs the full procedure; project restored to original phase on resolve.
4. **Three-routing direct path (option c):** small fix or exploration; user picks (c); agent handles without project creation or phase tracking.
5. **Skip-the-ask mid-arc:** project at `tdd:green`; user says "let's add another test for X"; agent does NOT ask (mid-arc rule); proceeds with `dev:tdd-red` or continues current TDD work.
6. **Soft wall bypass:** project at `design:open`; user invokes `dev:plan`; agent confirms bypass; `log-bypass` writes a row; plan work proceeds; phase remains `design:open` until explicit advancement.
7. **Multi-surface redundancy:** SessionStart hook disabled, CLAUDE.md snippet present; verify agent still has trigger contract via the CLAUDE.md fallback.
8. **Handoff retro:** project closed via `dev:handoff` after several bypasses; verify retro output enumerates the bypasses with phase/skill detail.

### 5.3 Open questions

1. **`--force` flag scope.** Spec mentions `--force` as an option to skip interactive confirmation on bypass. Should this be per-skill or a single global flag? Recommendation: single global `--force-bypass` flag on the calling skill, recorded in `phase_bypasses.note` as "forced (non-interactive)". TBD: confirm with implementer at plan stage.
2. **Bypass idempotency window.** Section 3.4 proposes a one-minute idempotency window for `log-bypass`. Is one minute right? Alternatives: 10 seconds (less forgiving), or hash-based dedup on (project, skill, phase) within session. Recommendation: stick with one minute as the simplest workable default; revisit if false-positive bypasses appear.
3. **Should diagnose use `phase_bypasses` at all?** §3.6 says no — diagnose has its own session-record discipline. But a future retro tool might want a unified "out-of-flow events" view. Recommendation: leave separate for now; if a unified view is needed later, surface both in `dev:handoff` retro output.
4. **CLAUDE.md snippet — paste-in vs @-import.** §4.1 says "consumer pastes into their project's CLAUDE.md." An alternative is to ship `templates/CLAUDE-snippet.md` and tell consumers to `@-import` it from their CLAUDE.md (e.g., `@.atelier/templates/CLAUDE-snippet.md`). Recommendation: paste-in for now (simpler install). @-import is a follow-up if many consumers ask for it.
5. **Detection edge case — "implement Y" inside a question.** "How would you implement Y in this codebase?" — is this a question or a work request? The agentic-agile manifesto would say: it depends on whether the user expects code at the end. Recommendation: agent should ask one clarifying question ("Are you asking how it would work, or asking me to do it?") before firing the three-routing ask. Add to Red Flags table.
6. **`session_start.py` and Windows path handling.** Hook reads `skills/using-atelier/SKILL.md` via a path computed from `__file__`. Cross-platform path safety needs to match existing `session_open.py` patterns. Recommendation: use `pathlib.Path` exclusively, mirror existing hook code, test on Windows.
7. **Bypass-aware advancement.** When a user has bypassed several gates and then explicitly advances (e.g., from `design:open` to `design:approved`), should the advancement warn about un-acknowledged bypasses? Recommendation: out of scope for v0.2.0; revisit when bypass patterns are observed in real use.
8. **What `dev:design` should look like under soft walls.** Step 7 (`advance to design:approved`) currently fires unconditionally. Should it ask the user "approve this design now?" first? It already does (step 4 asks for explicit human approval before advancing). No change needed — the soft wall framework doesn't alter design's existing approval gate.
9. **Self-review step in `dev:design` and `dev:plan`.** Superpowers' `brainstorming` skill has a built-in spec self-review (placeholder scan, internal consistency, scope check, ambiguity check) before requesting human approval. Atelier has no analogue. Recommendation: defer to a separate spec. Adding inline self-review to every dev-skill procedure expands the current scope unhelpfully; a dedicated `dev:self-review` skill (or shared self-review pattern referenced from `using-atelier/SKILL.md`) is the cleaner design and warrants its own brainstorm.

### 5.4 Non-goals reiterated

- Wave-based parallelism is not in this spec. Soft walls are a *precondition* for wave-based work (you can't have multiple stories at different phases under hard walls), but the wave model itself is a separate design.
- Removing phases is not in scope. Phases are still useful state tracking.
- Replacing `workflow.py advance` is not in scope. Explicit advancement remains the canonical state transition.

---

## End of spec — next steps

After spec self-review and your approval, this spec moves to `superpowers:writing-plans` for an implementation plan. The plan will decompose Section 4's deliverables into ordered tasks with explicit file ownership, suitable for `superpowers:subagent-driven-development` execution.

---
name: using-atelier
description: Use when starting any session in a project that uses Atelier — establishes the trigger contract for new-work requests and the soft-wall bypass procedure.
---

Atelier is a workspace and methodology for a human developer collaborating with one or more AI agents on a software project. This skill defines the trigger contract every session follows and the bypass procedure for soft phase walls.

## Trigger contract

On every user message, before responding:

1. **Mid-arc rule.** If a project is active and its phase is not `handoff:complete`, continue the current arc. Do NOT ask. Proceed with the phase-recommended skill (see Phase guidance) or with the user's explicit request.
2. **No-fire rule.** If the message is a question, exploration, read-only request, or trivial edit (see Red Flags), handle directly without asking.
3. **Ask gate.** If the message describes new development work, ask the user one of three routings:
   - **(a) Full Atelier arc** — invoke `project:create`, then `dev:design`. Routes through design → plan → tdd → review → security → qa → handoff with soft walls.
   - **(b) Bug fix** — invoke `dev:diagnose` against the active project. Captures pre-diagnose phase, writes regression test first, restores phase on resolution.
   - **(c) Handle directly** — do the work without Atelier orchestration. No project created, no phase tracked.

Wait for an explicit user response. Default to (a) if the user says "yes" without specifying.

## Red Flags

| Thought | Reality |
|---|---|
| "User just wants a quick fix" | Quick fixes still go through option (b). Ask. |
| "This is too small to need design" | Ask. User can pick option (c). |
| "User is asking a question, no need to ask" | Correct — questions don't fire. Only work requests fire. |
| "Project is already active, no need to ask" | Correct — don't re-ask mid-arc. Continue current phase. |
| "User said 'how do I X' so it's a question" | Verify: are they asking how, or asking the agent to do it? Latter fires. |
| "User said 'rename X to Y' — it's a tiny edit" | Tiny mechanical edits do not fire. Substantive renames (refactors affecting >5 files) fire. |
| "Refactor isn't new work" | Substantive refactors are new work. They get specs and reviews. Ask. |

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
| `design:open` | Continue grilling. Do not write code yet. | `dev:design` |
| `design:approved` | Draft the implementation plan. | `dev:plan` |
| `plan:open` | Continue refining the plan with the user. | `dev:plan` |
| `plan:approved` | Write the first failing test. | `dev:tdd` |
| `tdd:red` | Write minimal implementation to make tests pass. | `dev:tdd` |
| `tdd:green` | Refactor with tests still passing. | `dev:tdd` |
| `tdd:clean` | Continue TDD (new test) or advance to review. | `dev:tdd` or `dev:review` |
| `review:open` | Address findings or mark as approved. | `dev:review` |
| `review:changes-requested` | Apply requested changes, then re-review. | `dev:review` |
| `review:approved` | Run security review. | `dev:security` |
| `security:open` | Apply security findings or mark approved. | `dev:security` |
| `security:changes-requested` | Apply security findings, then re-review. | `dev:security` |
| `security:approved` | Run QA review. | `dev:qa` |
| `qa:open` | Address QA findings or mark approved. | `dev:qa` |
| `qa:changes-requested` | Apply QA findings, then re-review. | `dev:qa` |
| `qa:approved` | Close out the project. | `dev:handoff` |
| `diagnose:open` | Reproduce the bug, write regression test, fix root cause. | `dev:diagnose` |
| `diagnose:resolved` | Restore to pre-diagnose phase. | `dev:diagnose` (final steps) |
| `handoff:complete` | Project is closed. New work requires a new project. | — |

## Dev arc

The canonical Atelier development flow:

```
design → plan → tdd (red ⇄ green ⇄ clean) → review → security → qa → handoff
              ↑
              └── diagnose (entered from any non-terminal phase, restored on resolve)
```

All transitions are tracked in `atelier.db` (`projects.phase` column). Transitions are validated by `scripts/workflow.py advance` against the `phase_transitions` table. Skills no longer block on out-of-phase invocation — instead they apply the Bypass procedure below.

## Bypass procedure

Every dev skill's step 1 follows this pattern:

1. Call `python scripts/workflow.py <db_path> check-gate <project_id> <skill>`. Parse the JSON output. The fields are: `allowed` (bool), `current_phase` (str), `required_phase` (str | null), `reason` (str).
2. **If `allowed` is true:** proceed with the skill's procedure.
3. **If `allowed` is false:**
   - Display to the user: *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*
   - On **yes:** call `python scripts/workflow.py <db_path> log-bypass <project_id> <skill> <current_phase> <required_phase>` (optionally with `--agent <agent_id>` and `--note "<reason>"`), then proceed with the skill's procedure.
   - On **no:** stop. Tell the user: *"Advance to `<required_phase>` first (run `python scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*

Bypass entries are recorded in the `phase_bypasses` table and surfaced by `dev:handoff` during retrospective.

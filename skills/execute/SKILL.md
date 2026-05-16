---
description: Use when starting any session in a project that uses Atelier — establishes the trigger contract for new-work requests and the soft-wall bypass procedure.
---

<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific task (e.g., via `internal/dev-subagent/SKILL.md`) — identifiable by a briefing header such as "You are an implementer subagent" or "You have been dispatched to complete one task" — skip the Trigger contract below entirely. Apply the Bypass procedure normally; phase gates still govern your work. You are executing a bounded task within an already-active arc; do not re-initiate the Ask gate.
</SUBAGENT-STOP>

Atelier is a workspace and methodology for a human developer collaborating with one or more AI agents on a software project. This skill defines the trigger contract every session follows and the bypass procedure for soft phase walls.

> **Session-open requirement.** On the first message of every session in an Atelier project: verify Memex is present, identify the active project and its current phase, then select the phase-recommended procedure from Phase guidance before responding to any user request.

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

1. Call `python atelier/scripts/workflow.py <db_path> check-gate <project_id> <skill>`. Parse the JSON output. The fields are: `allowed` (bool), `current_phase` (str), `required_phase` (str | null), `reason` (str).
2. **If `allowed` is true:** proceed with the skill's procedure.
3. **If `allowed` is false:**
   - Display to the user: *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*
   - On **yes:** call `python atelier/scripts/workflow.py <db_path> log-bypass <project_id> <skill> <current_phase> <required_phase>` (optionally with `--agent <agent_id>` and `--note "<reason>"`), then proceed with the skill's procedure.
   - On **no:** stop. Tell the user: *"Advance to `<required_phase>` first (run `python atelier/scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*

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
| `plan:approved` (parallel tasks) | Dispatch fresh subagents per task with two-stage review instead of implementing directly. | `internal/dev-subagent/SKILL.md` |
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
              ├── dev-subagent (alternative to dev-tdd; enters at plan:approved, exits at tdd:clean)
              └── diagnose (entered from any non-terminal phase, restored on resolve)
```

All transitions are tracked in `memex.db` (`projects.phase` column). Transitions are validated by `atelier/scripts/workflow.py advance` against the `phase_transitions` table. Skills no longer block on out-of-phase invocation — instead they apply the Bypass procedure above.

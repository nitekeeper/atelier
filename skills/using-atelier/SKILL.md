---
name: atelier:using-atelier
description: Use when starting any session in a project that uses Atelier — establishes the trigger contract for new-work requests and the soft-wall bypass procedure.
---

<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific task (e.g., via `dev:subagent`) — identifiable by a briefing header such as "You are an implementer subagent" or "You have been dispatched to complete one task" — skip the Trigger contract below entirely. Apply the Bypass procedure normally; phase gates still govern your work. You are executing a bounded task within an already-active arc; do not re-initiate the Ask gate.
</SUBAGENT-STOP>

Atelier is a workspace and methodology for a human developer collaborating with one or more AI agents on a software project. This skill defines the trigger contract every session follows and the bypass procedure for soft phase walls.

> **Session-open requirement.** On the first message of every session in an Atelier project: verify Memex is present, identify the active project and its current phase, then select the phase-recommended skill from Phase guidance before responding to any user request.

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
   - **(a) Full Atelier arc** — invoke `project:create`, then `dev:design`. Routes through design â†’ plan â†’ tdd â†’ review â†’ security â†’ qa â†’ handoff with soft walls.
   - **(b) Bug fix** — invoke `dev:diagnose` against the active project. Captures pre-diagnose phase, writes regression test first, restores phase on resolution.
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

Bypass entries are recorded in the `phase_bypasses` table and surfaced by `dev:handoff` during retrospective.

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
| "I'll verify later, it looks right" | "Looks right" is not evidence. Run `dev:verify` now. Later means never. |

**Firing patterns (examples):**
- "I want to add X" â†’ fires
- "Build a system that does Y" â†’ fires
- "The bug in Z is back" â†’ fires (option b recommended)
- "Refactor the auth module" â†’ fires
- "How does this codebase handle X?" â†’ does not fire (question)
- "Show me the file at path Y" â†’ does not fire (read-only)
- "Fix the typo on line 42" â†’ does not fire (trivial edit)
- "List the open tasks" â†’ does not fire (CRUD)

## Phase guidance

| Phase | Recommended next action | Skill |
|---|---|---|
| `design:open` | Continue grilling. Do not write code yet. | `dev:design` |
| `design:approved` | Draft the implementation plan. | `dev:plan` |
| `plan:open` | Continue refining the plan with the user. | `dev:plan` |
| `plan:approved` | Write the first failing test (single-agent). | `dev:tdd` |
| `plan:approved` (parallel tasks) | Dispatch fresh subagents per task with two-stage review instead of implementing directly. | `dev:subagent` |
| `tdd:red` | Write minimal implementation to make tests pass. | `dev:tdd` |
| `tdd:green` | Verify tests pass (vacuity check, full output read), then refactor with tests still passing. | `dev:verify`, then `dev:tdd` |
| `tdd:clean` | Verify suite is clean, then continue TDD (new test) or advance to review. | `dev:verify`, then `dev:tdd` or `dev:review` |
| `review:open` | Address findings or mark as approved. | `dev:review` |
| `review:changes-requested` | Read all feedback, classify each item (accept / clarify / push-back), implement accepted fixes, re-request review. | `dev:receive-review` |
| `review:approved` | Run security review. | `dev:security` |
| `security:open` | Apply security findings or mark approved. | `dev:security` |
| `security:changes-requested` | Apply security findings, then re-review. | `dev:security` |
| `security:approved` | Run QA review. | `dev:qa` |
| `qa:open` | Address QA findings or mark approved. | `dev:qa` |
| `qa:changes-requested` | Apply QA findings, then re-review. | `dev:qa` |
| `qa:approved` | Run pre-flight checks, confirm CI green, choose integration path (merge / PR / abandon). | `dev:finish` |
| `handoff:open` | Integration artefact exists. Write session record and advance to complete. | `dev:finish` (step 5) |
| `diagnose:open` | Reproduce the bug, write regression test, fix root cause. | `dev:diagnose` |
| `diagnose:resolved` | Restore to pre-diagnose phase. | `dev:diagnose` (final steps) |
| `handoff:complete` | Project is closed. New work requires a new project. | — |

### Cross-cutting skills (any phase)

| Condition | Recommended next action | Skill |
|---|---|---|
| Before any phase advance where tests must pass | Run 5-step gate: identify tests, run suite, read full output, vacuity check, claim pass/fail. | `dev:verify` |
| Authoring new Atelier infrastructure | Author, review, and register a new skill. Does not require or advance any project phase. | `dev:write-skill` |

## Dev arc

The canonical Atelier development flow:

```
design â†’ plan â†’ tdd (red â‡„ green â‡„ clean) â†’ review â†’ security â†’ qa â†’ handoff
              â†‘
              â”œâ”€â”€ dev:subagent (alternative to dev:tdd; enters at plan:approved, exits at tdd:clean)
              â””â”€â”€ diagnose (entered from any non-terminal phase, restored on resolve)
```

All transitions are tracked in `memex.db` (`projects.phase` column). Transitions are validated by `atelier/scripts/workflow.py advance` against the `phase_transitions` table. Skills no longer block on out-of-phase invocation — instead they apply the Bypass procedure above.

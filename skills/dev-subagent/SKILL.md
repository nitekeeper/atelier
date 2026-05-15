---
name: atelier-dev-subagent
description: Use when executing an approved implementation plan — dispatches fresh subagents per task with two-stage review (spec compliance then code quality).
---

# dev:subagent

Executes an approved plan by dispatching a fresh implementer subagent per task, followed by a spec-compliance reviewer and a code-quality reviewer. The coordinator never implements; it orchestrates and verifies. Runs without human check-ins between tasks unless a stopping condition is met.

## Hard gate

Requires `plan:approved`.

## Procedure

1. **Check the phase gate:**
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:subagent
   ```
   Apply standard bypass-confirm-log flow if `allowed` is `false`.

2. **Load the plan.** Read the plan document:
   ```
   python atelier/scripts/documents.py list --project_id <project_id>
   ```
   Open the plan file. List all tasks:
   ```
   python atelier/scripts/tasks.py list --project_id <project_id> --status pending
   ```
   Work tasks in order. If any task is tagged `[DESTRUCTIVE]` in its description, note it — these require explicit human confirmation before dispatch (step 4b).

3. **Human checkpoint at task ceiling.** If the plan contains more than 10 tasks, pause now:
   > "This plan has [N] tasks. After task 10 I will pause for a human checkpoint. Proceeding."

4. **For each task (in order):**

   **a. Claim:**
   ```
   python atelier/scripts/tasks.py claim <task_id> subagent-implementer
   python atelier/scripts/workflow.py <db_path> advance <project_id> tdd:red
   ```

   **b. Destructive gate.** If the task is tagged `[DESTRUCTIVE]`: pause and ask:
   > "Task [N] is tagged destructive: [description]. Confirm dispatch? (yes/no)"
   Wait for yes before continuing.

   **c. Dispatch implementer subagent** using `implementer-prompt.md` as the briefing template. Provide: plan task text, relevant file paths, test name from the plan, project context.

   - If the subagent returns **BLOCKED**: stop the chain immediately. Report to user: "Chain halted at task [N]: [blocking reason]. Resolve before resuming."
   - If the subagent returns a non-zero exit or error: stop the chain immediately.

   **d. Advance to review:**
   ```
   python atelier/scripts/workflow.py <db_path> advance <project_id> tdd:clean
   ```

   **e. Stage 1 — Spec compliance review.** Dispatch a spec-reviewer subagent using `spec-reviewer-prompt.md`. Provide: the plan task, the diff of changes made.
   - Pass: continue to Stage 2.
   - Fail: re-dispatch the implementer with the spec gaps noted (max 2 retries). If still failing after 3 total attempts: stop chain. Surface to user.

   **f. Stage 2 — Code quality review.** Dispatch a quality-reviewer subagent using `quality-reviewer-prompt.md`. Provide: the full changed files.
   - Pass: complete the task.
   - Fail: re-dispatch the implementer with the quality issues noted (max 1 retry). If still failing: stop chain. Surface to user.

   **g. Complete:**
   ```
   python atelier/scripts/tasks.py complete <task_id>
   ```

   **h. Human checkpoint (if task 10 reached).** Pause:
   > "Completed tasks 1–10. [N] tasks remain. Continue? (yes/no)"

5. **After all tasks complete:**
   ```
   pytest -v
   ```
   All tests must pass. Report: "All [N] tasks complete. Phase: tdd:clean. Invoke `dev:review` to begin code review."
   Do not advance to `review:open` — that is the human's call.

## Hard rules

- Never implement directly — the coordinator dispatches subagents only.
- Hard stop on any non-recoverable subagent error — do not continue to the next task.
- Destructive tasks require explicit human confirmation before dispatch.
- Human checkpoint is mandatory at task 10 — do not skip it regardless of success streak.
- Never advance to `review:open` — leave that for `dev:review`.
- Stage 1 max 3 total dispatch attempts; Stage 2 max 2. Exceed either: stop and surface.

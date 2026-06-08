---
description: Use when executing an approved implementation plan — dispatches fresh subagents per task with two-stage review (spec compliance then code quality).
---

# dev:subagent

Executes an approved plan by dispatching a fresh implementer subagent per task, followed by a spec-compliance reviewer and a code-quality reviewer. The coordinator never implements; it orchestrates and verifies. Runs without human check-ins between tasks unless a stopping condition is met.

> **Prerequisites**
> - Mode: Memex or Local (mode-symmetric — `workflow.py` + `tasks.py` + `documents.py` dispatch via `backend.py`)
> - Required: `plan:approved` phase reached
> - Required tables: `projects`, `skill_gates`, `phase_bypasses`, `tasks`, `project_documents` — seeded by Atelier bootstrap

## Hard gate

Requires `plan:approved`.

## Procedure

1. **Check the phase gate:**
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:subagent
   ```
   Apply standard bypass-confirm-log flow if `allowed` is `false`.

2. **Load the plan.** Read the plan document:
   ```
   python3 atelier/scripts/documents.py list --project_id <project_id>
   ```
   Open the plan file. List all tasks:
   ```
   python3 atelier/scripts/tasks.py list --project_id <project_id> --status pending
   ```
   Work tasks in order. If any task is tagged `[DESTRUCTIVE]` in its description, note it — these require explicit human confirmation before dispatch (step 4b).

2a. **Loom chat kickoff (availability-gated).** Before dispatching the first task, probe Loom and open a session channel for the subagent chain. This is the **same availability gate** used in `internal/dev-dispatch/SKILL.md` step 3b — the only difference is that subagents run sequentially, so `individual_goals` and `members` are empty at kickoff (each subagent is registered at dispatch time, not pre-announced).

   ```python
   from scripts.loom_comms import (
       detect, build_team_chat_context, kickoff, teardown,
   )
   status = detect()                              # never raises; fail-soft
   channel = f"dev-subagent-{<project_id>}"
   if status.available:
       kickoff(
           status=status, channel=channel,
           team_goal=<one-line task-chain objective>,
           individual_goals={},   # sequential — no concurrent members to pre-announce
           members=[],
       )
   ```

   For **each subagent dispatch** (implementer, spec-reviewer, quality-reviewer), build
   the per-role chat ctx and render the `{{loom_section}}` to inject into the briefing:
   ```python
   team_chat = build_team_chat_context(
       status,
       role_id=<subagent_role_id>,   # e.g. "implementer", "spec-reviewer", "quality-reviewer"
       channel=channel,
       team_lead_name=<orchestrator_name>,
   )
   if team_chat["transport"] == "loom":
       cmds = team_chat["cmds"]
       loom_section = f"""## Loom agent-chat

Use Loom to send status to the team-lead or check your inbox.

| Action | Command |
|---|---|
| Register | `{cmds["register"]}` |
| Send to team-lead | `{cmds["send_to_lead"]}` |
| Check inbox | `{cmds["read_inbox"]}` |
| Mark read | `{cmds["mark_read"]}` |
| Deregister (REQUIRED before returning terminal status) | `{cmds["deregister"]}` |

{cmds["doc_spill"]}

**Deregister BEFORE you return your terminal status (COMPLETE / BLOCKED / PASS / FAIL).**
"""
   else:
       loom_section = ""
   ```
   Inject the resulting `loom_section` string into the briefing template's `{{loom_section}}` placeholder.

3. **Human checkpoint at task ceiling.** If the plan contains more than 10 tasks, pause now:
   > "This plan has [N] tasks. After task 10 I will pause for a human checkpoint. Proceeding."

4. **For each task (in order):**

   **a. Claim:**
   ```
   python3 atelier/scripts/tasks.py claim <task_id> subagent-implementer
   python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:red
   ```

   **b. Destructive gate.** If the task is tagged `[DESTRUCTIVE]`: pause and ask:
   > "Task [N] is tagged destructive: [description]. Confirm dispatch? (yes/no)"
   Wait for yes before continuing.

   **c. Dispatch implementer subagent** using `implementer-prompt.md` as the briefing template. Provide: plan task text, relevant file paths, test name from the plan, project context, and `loom_section` (built in step 2a with `role_id="implementer"` — empty string when Loom unavailable).

   - If the subagent returns **BLOCKED**: stop the chain immediately. Report to user: "Chain halted at task [N]: [blocking reason]. Resolve before resuming."
   - If the subagent returns a non-zero exit or error: stop the chain immediately.

   **d. Advance to review:**
   ```
   python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:clean
   ```

   **e. Stage 1 — Spec compliance review.** Dispatch a spec-reviewer subagent using `spec-reviewer-prompt.md`. Provide: the plan task, the diff of changes made, and `loom_section` (built with `role_id="spec-reviewer"`).
   - Pass: continue to Stage 2.
   - Fail: re-dispatch the implementer with the spec gaps noted (max 2 retries). If still failing after 3 total attempts: stop chain. Surface to user.

   **f. Stage 2 — Code quality review.** Dispatch a quality-reviewer subagent using `quality-reviewer-prompt.md`. Provide: the full changed files, and `loom_section` (built with `role_id="quality-reviewer"`).
   - Pass: complete the task.
   - Fail: re-dispatch the implementer with the quality issues noted (max 1 retry). If still failing: stop chain. Surface to user.

   **g. Complete:**
   ```
   python3 atelier/scripts/tasks.py complete <task_id>
   ```

   **h. Human checkpoint (if task 10 reached).** Pause:
   > "Completed tasks 1–10. [N] tasks remain. Continue? (yes/no)"

5. **After all tasks complete:**
   ```
   pytest -v
   ```
   All tests must pass. Report: "All [N] tasks complete. Phase: tdd:clean. Invoke `internal/dev-review/SKILL.md` to begin code review."
   Do not advance to `review:open` — that is the human's call.

6. **Loom teardown.** Deregister all Loom participants so no subagent stays registered in the channel:
   ```python
   teardown(status=status, members=[])
   ```
   Fail-soft, idempotent, no-op when Loom was unavailable. Belt-and-suspenders with each subagent's own `deregister` call before it returns its terminal status.

## Hard rules

- Never implement directly — the coordinator dispatches subagents only.
- Hard stop on any non-recoverable subagent error — do not continue to the next task.
- Destructive tasks require explicit human confirmation before dispatch.
- Human checkpoint is mandatory at task 10 — do not skip it regardless of success streak.
- Never advance to `review:open` — leave that for `internal/dev-review/SKILL.md`.
- Stage 1 max 3 total dispatch attempts; Stage 2 max 2. Exceed either: stop and surface.

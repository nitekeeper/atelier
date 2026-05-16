---
description: Use when a design is approved and ready for implementation — translates the design into a concrete task list.
user-invocable: false
---

# dev:plan

Implementation planning. Translates an approved design into a concrete task list. Produces a plan document that guides TDD execution.

## Hard gate

Requires `design:approved`.

## Procedure

1. Check the phase gate:
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:plan
   ```
   Parse the JSON output: `{"allowed": bool, "current_phase": str, "required_phase": str, "reason": str}`.

   **If `allowed` is `true`**: record `current_phase` and proceed to the next step.

   **If `allowed` is `false`** (soft wall): ask the user:

   > *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*

   - On **yes**: run:
     ```
     python atelier/scripts/workflow.py <db_path> log-bypass <project_id> dev:plan <current_phase> <required_phase>
     ```
     Optionally append `--agent <agent_id>` and `--note "<reason>"`. Then proceed to the next step.
   - On **no**: stop. Tell the user:
     > *"Advance to `<required_phase>` first (run `python atelier/scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*

2. Advance phase: `python atelier/scripts/workflow.py <db_path> advance <project_id> plan:open`

3. Read the approved design document for the project:
   ```
   python atelier/scripts/documents.py list --project_id <project_id>
   ```
   This returns JSON with all documents. Extract the `filename` field for the design document (type: "design") and read that file. Do not plan without reading the approved design.

4. Write the implementation plan to `docs/plans/<project-slug>-plan.md`.

   Plan structure:
   - **Goal** — one sentence
   - **Tech constraints** — list any mandated libraries, languages, or patterns from the design
   - **Tasks** — numbered list. Each task must have:
     - Title
     - File(s) to create or modify (exact paths)
     - Failing test first (test name + assertion)
     - Implementation step
     - "Run tests" step
     - Commit message

   Rules:
   - Every task produces a passing test.
   - Tasks are ordered by dependency. No task depends on an unbuilt component.
   - No placeholders. Every step is complete enough to execute without asking questions.

5. Register the plan document:
   ```
   python atelier/scripts/documents.py create <project_id> plan "<title>" "<filename>" "<agent_id>"
   ```

6. When plan is approved by the human:
   - Advance phase: `python atelier/scripts/workflow.py <db_path> advance <project_id> plan:approved`
   - Confirm: "Plan approved. Phase: plan:approved. Ready to begin dev:tdd."

## Hard rules
- Never begin planning without reading the approved design — plans written without the design are invalid.
- Every task in the plan must include a test. Testless tasks are rejected.
- Do not advance to `plan:approved` without explicit human approval.

# dev:plan

Implementation planning. Translates an approved design into a concrete task list. Produces a plan document that guides TDD execution.

## Hard gate

Requires `design:approved`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:plan`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> plan:open`

3. Read the approved design document for the project:
   ```
   python atelier/scripts/documents.py list <project_id>
   ```
   Open the design document. Do not plan without reading it.

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
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> plan:approved`
   - Confirm: "Plan approved. Phase: plan:approved. Ready to begin dev:tdd."

## Hard rules
- Never begin planning without reading the approved design — plans written without the design are invalid.
- Every task in the plan must include a test. Testless tasks are rejected.
- Do not advance to `plan:approved` without explicit human approval.

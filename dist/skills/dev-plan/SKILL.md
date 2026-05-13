# dev:plan

Produces the implementation plan from the approved design document. Breaks the design into ordered tasks. Identifies the vertical slice.

## Hard gate

Requires `design:approved`. The skill refuses if the project is not at this phase.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:plan`
   If the gate fails, state the current phase and stop.

2. Retrieve the design document: `python atelier/scripts/documents.py list --project_id <project_id> --type design`
   Read the design document file.

3. Advance project to plan phase: `python atelier/scripts/workflow.py advance <project_id> plan:in-progress`

4. Decompose the design into an ordered task list. Each task must be:
   - Completable in a single coding session
   - Independently reviewable
   - Described as a verb phrase: "Add X", "Refactor Y", "Remove Z"
   - Refactoring tasks separated from feature tasks (never mixed in the same task)

5. Identify the **vertical slice**: the minimum set of tasks that produces an end-to-end observable result. Mark it explicitly.

6. If the task list exceeds 10 items: flag this to the human before proceeding. The design scope may be too large.

7. Present the plan to the human for review. Ask: "Does this plan match the design? What should change?"
   Revise until the human explicitly approves.

8. Write the implementation plan to a file (e.g. `docs/plans/<project-slug>-plan.md`).

9. Register the document: `python atelier/scripts/documents.py create <project_id> implementation-plan "<title>" "<filename>" "<agent_id>"`

10. Advance phase: `python atelier/scripts/workflow.py advance <project_id> plan:approved`

11. Confirm: "Implementation plan approved. Phase advanced to plan:approved. Ready for `dev:tdd-red`."

## Hard rules
- Refactoring and feature implementation are separate tasks. Never mix them.
- Never advance the phase without explicit human approval.
- If >10 tasks: stop and flag to the human before proceeding.

# dev:handoff

Serializes current session state to `.ai/work.md`. Callable from any phase. Always the last action before closing a session.

## Hard gate

None — callable from any phase.

## Procedure

1. Determine current project state:
   - `python atelier/scripts/workflow.py get-phase <project_id>`
   - `python atelier/scripts/tasks.py list --project_id <project_id>`

2. Run: `python atelier/scripts/session.py write .ai/work.md`
   Provide:
   - `current-task`: one sentence — what is in progress right now
   - `status`: in-progress | blocked | complete
   - `blocking-reason`: what is blocking (if status is blocked)
   - `accomplished`: what was done this session
   - `next-action`: first imperative action for the next session

3. Confirm: "Session state saved to `.ai/work.md`. Next action: [next-action]."

4. Ask: "Anything to capture to the knowledge base before closing? (y/n)"
   If yes: invoke `ingest`.

## Hard rules
- `next-action` must be a specific imperative sentence — not "continue" or "resume". It must name the exact action (e.g., "Run `dev:tdd-green` for project 3").
- Always run dev:handoff before ending any session on a project.

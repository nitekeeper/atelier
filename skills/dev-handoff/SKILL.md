# dev:handoff

Records current session state to the DB. Callable from any phase. Always the last action before closing a session.

## Hard gate

None — callable from any phase.

## Procedure

1. Check the phase gate:
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:handoff
   ```
   Parse the JSON output `{"allowed": bool, "current_phase": str, "required_phase": str|null, "reason": str}`.
   For this skill `allowed` is always `true` (no gate configured). Record `current_phase` for later use, then proceed to the next step.
   - If the project does not exist, stop and tell the user to create one first with `project:create`.

   Determine current project state:
   ```
   python atelier/scripts/workflow.py get-phase <project_id>
   python atelier/scripts/tasks.py list --project_id <project_id>
   ```

2. Write session state:
   ```
   python atelier/scripts/session.py write <project_id> <agent_id> <current_phase> <status> \
     --accomplished "<what was completed this session>" \
     --next-action "<exact first action for the next session>" \
     [--notes "<pm notes for the next session>"] \
     [--blocking-reason "<what is blocking, if status is blocked>"]
   ```

   Where:
   - `<current_phase>`: result of `workflow.py get-phase`
   - `<status>`: `in-progress`, `blocked`, or `complete`
   - `--next-action`: specific imperative sentence naming the exact action (e.g. "Run `dev:tdd` for project 3")
   - `--blocking-reason` is required when `<status>` is `blocked`

3. Confirm: "Session state recorded. Next action: [next action]."

4. Ask: "Anything to capture to the knowledge base before closing? (y/n)"
   If yes: invoke `ingest`.

## Hard rules
- `--next-action` must be a specific imperative sentence — not "continue" or "resume".
- Always run dev:handoff before ending any session on a project.
- Status `blocked` requires `--blocking-reason` to be set.

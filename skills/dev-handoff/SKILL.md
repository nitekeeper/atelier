---
name: atelier-dev-handoff
description: Use at the end of any session — records current state to the DB so the next session can resume.
---

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
   - `<current_phase>`: captured from check-gate JSON response in step 1
   - `<status>`: `in-progress`, `blocked`, or `complete`
   - `--next-action`: specific imperative sentence naming the exact action (e.g. "Run `dev:tdd` for project 3")
   - `--blocking-reason` is required when `<status>` is `blocked`

3. Confirm: "Session state recorded. Next action: [next action]."

4. **Query phase bypasses for retro:**

   ```python
   from contextlib import closing
   from scripts.db import get_connection

   with closing(get_connection('<db_path>')) as conn:
       rows = conn.execute('''
           SELECT skill, current_phase, required_phase, COUNT(*) AS n,
                  GROUP_CONCAT(note, ' | ') AS notes
           FROM phase_bypasses
           WHERE project_id = ?
           GROUP BY skill, current_phase, required_phase
           ORDER BY n DESC
       ''', (<project_id>,)).fetchall()
       for row in rows:
           print(row)
   ```

   Format the output as a **Bypasses** subsection in the retro:

   - For each row: `<skill>: <n> bypass(es) from <current_phase> (normally requires <required_phase>)`. If `notes` is non-empty, append it.
   - If no rows, write: "*No phase bypasses during this project's lifecycle.*"

5. Ask: "Anything to capture to the knowledge base before closing? (y/n)"
   If yes: invoke `ingest`.

## Hard rules
- `--next-action` must be a specific imperative sentence — not "continue" or "resume".
- Always run dev:handoff before ending any session on a project.
- Status `blocked` requires `--blocking-reason` to be set.

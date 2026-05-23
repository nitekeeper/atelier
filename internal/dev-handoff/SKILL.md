---
description: Use at the end of any session — records current state to the DB so the next session can resume.
---

# dev:handoff

Records current session state to the DB. Callable from any phase. Always the last action before closing a session.

> **Prerequisites**
> - Mode: Memex or Local (mode-symmetric — phase state routed via `backend.py`)
> - Required: an existing project (created via `internal/project/SKILL.md`)
> - Required tables: `projects`, `sessions`, `phase_bypasses` — seeded by Atelier bootstrap

## Hard gate

None — callable from any phase.

## Procedure

1. Check the phase gate:
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:handoff
   ```
   Parse the JSON output `{"allowed": bool, "current_phase": str, "required_phase": str|null, "reason": str}`.
   For this skill `allowed` is always `true` (no gate configured). Record `current_phase` for later use, then proceed to the next step.
   - If the project does not exist, stop and tell the user to create one first with `internal/project/SKILL.md` (`create`).

   Determine current project state:
   ```
   python3 atelier/scripts/tasks.py list --project_id <project_id>
   ```

2. Write session state:
   ```
   python3 atelier/scripts/session.py write <project_id> <agent_id> <current_phase> <status> \
     --accomplished "<what was completed this session>" \
     --next-action "<exact first action for the next session>" \
     [--notes "<pm notes for the next session>"] \
     [--blocking-reason "<what is blocking, if status is blocked>"]
   ```

   Where:
   - `<current_phase>`: captured from check-gate JSON response in step 1
   - `<status>`: `in-progress`, `blocked`, or `complete`
   - `--next-action`: specific imperative sentence naming the exact action (e.g. "Run `internal/dev-tdd/SKILL.md` for project 3")
   - `--blocking-reason` is required when `<status>` is `blocked`

3. Confirm: "Session state recorded. Next action: [next action]."

4. **Query phase bypasses for retro:**

   `scripts/backend.py` does not yet expose a read path for `phase_bypasses` (`list_phase_bypasses` is deferred — see the v1.2.0 block in `backend.py`). `scripts/workflow.py` exposes only a `log-bypass` write command, not a list command. Use the mode-appropriate snippet below directly.

   <!-- TODO: route via backend facade once list_phase_bypasses lands -->
   <!-- NOTE: the direct backend_local/_memex calls below violate CLAUDE.md rule 1
        ("Never call backend_memex.* or backend_local.* directly from a skill").
        This is an acknowledged temporary workaround until list_phase_bypasses is
        added to the backend facade. -->

   **Local mode** (`.ai/atelier.db` present, Memex not installed):
   ```python
   # Run as: python3 -c "<contents below>" (replace <db_path> and <project_id>)
   from contextlib import closing
   from scripts.backend_local import _conn

   with closing(_conn()) as conn:
       rows = conn.execute('''
           SELECT skill, current_phase, required_phase, COUNT(*) AS n,
                  GROUP_CONCAT(note, ' | ') AS notes
           FROM phase_bypasses
           WHERE project_id = ?
           GROUP BY skill, current_phase, required_phase
           ORDER BY n DESC
       ''', (<project_id>,)).fetchall()
       for row in rows:
           print(dict(row))
   ```

   **Memex mode** (Memex v2 installed):
   ```python
   # Run as: python3 -c "<contents below>" (replace <project_id>)
   from scripts.backend_memex import _memex_module

   stores = _memex_module("stores")
   rows = stores.query(
       "atelier",
       '''SELECT skill, current_phase, required_phase, COUNT(*) AS n,
                 GROUP_CONCAT(note, ' | ') AS notes
          FROM phase_bypasses
          WHERE project_id = ?
          GROUP BY skill, current_phase, required_phase
          ORDER BY n DESC''',
       (<project_id>,),
   )
   for row in rows:
       print(dict(row))
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

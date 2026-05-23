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

   ```python
   # Run as: python3 -c "<contents below>" (replace <project_id>)
   from collections import Counter
   from scripts import backend

   rows = backend.list_phase_bypasses(project_id=<project_id>)
   if not rows:
       print("No phase bypasses during this project's lifecycle.")
   else:
       # Aggregate by (from_phase, to_phase) for the retro display.
       counts = Counter((r["from_phase"], r["to_phase"]) for r in rows)
       reasons_by_pair = {}
       for r in rows:
           key = (r["from_phase"], r["to_phase"])
           reasons_by_pair.setdefault(key, []).append(r["reason"])
       for (from_phase, to_phase), n in counts.most_common():
           reasons = " | ".join(reasons_by_pair[(from_phase, to_phase)])
           print(f"from {from_phase} → {to_phase}: {n} bypass(es). Reasons: {reasons}")
   ```

   Format the output as a **Bypasses** subsection in the retro.

5. Ask: "Anything to capture to the knowledge base before closing? (y/n)"
   If yes: invoke `ingest`.

## Hard rules
- `--next-action` must be a specific imperative sentence — not "continue" or "resume".
- Always run dev:handoff before ending any session on a project.
- Status `blocked` requires `--blocking-reason` to be set.

---
description: Use when QA is approved and the work is ready to integrate — guides merge, PR, or abandon with pre-flight checks and phase completion.
---

# dev:finish

Final integration step. Runs after `qa:approved`. Verifies CI is green, presents integration options, and advances the project to `handoff:complete`.

> **Prerequisites**
> - Mode: Memex or Local (mode-symmetric — `workflow.py` + `session.py` dispatch via `backend.py`)
> - Required: `qa:approved` phase reached; working tree clean; CI green
> - Required tables: `projects`, `skill_gates`, `phase_bypasses`, `sessions` — seeded by Atelier bootstrap

## Hard gate

Requires `qa:approved`.

## Procedure

1. **Check the phase gate:**
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:finish
   ```
   Parse JSON output. If `allowed` is `false`, apply standard bypass-confirm-log flow before continuing.

2. **Pre-flight** (all blocking — do not skip):

   | Check | Command | Required result |
   |---|---|---|
   | Tests green | `pytest -v` | 0 failures — show full summary line |
   | Working tree clean | `git status --short` | No output |
   | No open assigned tasks | `python3 atelier/scripts/tasks.py list --project_id <project_id> --status assigned` | Empty list |
   | CI status | `gh run list --limit 1 --json status,conclusion` | `"conclusion": "success"` — show the output |

   If any check fails: stop. State what failed and what must be resolved. Do not advance phase.

3. **Advance phase:**
   ```
   python3 atelier/scripts/workflow.py <db_path> advance <project_id> handoff:open
   ```

4. **Present integration options** — ask the user to choose:

   > **How do you want to integrate this work?**
   > - **(a) Merge to main** — fast-forward or no-ff merge directly into the base branch. Requires your confirmation before executing.
   > - **(b) Open a PR** — push the branch and create a pull request for review before merging.
   > - **(c) Abandon** — discard the branch. Requires your explicit confirmation. Write a session note explaining why.

   Wait for the user's explicit choice before proceeding.

   **Option (a) — Merge to main:**
   Ask: "Confirm merge of `<branch>` into `<base>`? (yes/no)" Wait for yes before running:
   ```
   python3 atelier/scripts/worktree.py merge-back
   ```
   On non-zero exit: follow printed recovery instructions. Do not advance to `handoff:complete`.

   **Option (b) — Open a PR:**
   ```
   git push origin <branch>
   gh pr create --title "<project name>" --body "<summary from design doc>"
   ```
   Print the PR URL. Phase advances regardless — the PR is the integration artefact.

   **Option (c) — Abandon:**
   Ask: "Confirm abandoning branch `<branch>`? This deletes the branch. (yes/no)" Wait for yes, then:
   ```
   git branch -D <branch>
   ```
   Write a session note with the reason via `--notes "<reason>"` in step 5.

5. **Write session record and advance to complete:**
   ```
   python3 atelier/scripts/session.py write <project_id> <agent_id> handoff:open complete \
     --accomplished "<what was integrated>" \
     --next-action "Project complete — start a new project for follow-on work" \
     [--notes "<abandon reason or PR URL>"]
   ```
   Then advance:
   ```
   python3 atelier/scripts/workflow.py <db_path> advance <project_id> handoff:complete
   ```

6. **Retro — surface phase bypasses:**

   `scripts/backend.py` does not yet expose a read path for `phase_bypasses`. Use the mode-appropriate snippet directly.

   <!-- TODO: route via backend facade once list_phase_bypasses lands -->

   **Local mode** (`.ai/atelier.db` present):
   ```python
   # Run as: python3 -c "<contents below>" (replace <db_path> and <project_id>)
   from contextlib import closing
   from scripts.backend_local import _conn

   with closing(_conn()) as conn:
       rows = conn.execute(
           'SELECT skill, current_phase, required_phase, COUNT(*) AS n '
           'FROM phase_bypasses WHERE project_id = ? '
           'GROUP BY skill ORDER BY n DESC',
           (<project_id>,),
       ).fetchall()
       for row in rows: print(dict(row))
   ```

   **Memex mode** (Memex v2 installed):
   ```python
   # Run as: python3 -c "<contents below>" (replace <project_id>)
   from scripts.backend_memex import _memex_module

   rows = _memex_module("stores").query(
       "atelier",
       'SELECT skill, current_phase, required_phase, COUNT(*) AS n '
       'FROM phase_bypasses WHERE project_id = ? '
       'GROUP BY skill ORDER BY n DESC',
       (<project_id>,),
   )
   for row in rows: print(dict(row))
   ```

   If any bypasses exist, present the summary. Ask the user if any should be captured as lessons.

## Hard rules

- Never advance to `handoff:complete` before the integration action exits zero.
- Never execute merge or abandon without explicit user confirmation.
- Pre-flight is not skippable — all four checks must pass.
- CI check must show actual output, not be asserted from memory.
- `handoff:complete` is terminal — new work requires a new project.
- All three integration outcomes write a session record.

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

6. **(agent-team mode only) Record the team teardown:**

   > This step applies ONLY when this cycle ran in **agent-team mode** (a real
   > `team` was created — see `skills/run/SKILL.md`). Sub-agent / Memex finishes
   > create no team, so SKIP this step entirely and continue to the Retro. Scope
   > here is the TEARDOWN RECORD ONLY — do NOT add team-mode merge/PR/resume
   > logic to this section (that lives in a separate follow-up).

   On clean completion the team is torn down just like a deliberate abort, so
   `scripts/sweep_leaked_teams.py` does not later over-report it as an orphan.

   a. **Resolve the team_id.** Resolve it from this cycle's most-recent ready
      `create_team` bridge row (the canonical post-creation team_id lives in the
      RESPONSE), reusing `abort.resolve_team_id`'s query:
      ```python
      # Run as: python3 -c "<contents below>" (replace <team_pk>, <db_path>)
      from scripts.abort import resolve_team_id
      print(resolve_team_id("<db_path>", "<team_pk>") or "")
      ```
      `<team_pk>` is the cycle's run/cycle correlation id the orchestrator
      already holds for the bridge queue. A blank result means no team_id could
      be resolved — pass `--team-id` explicitly only if you already hold it.

   b. **Invoke the teardown record:**
      ```
      PYTHONPATH=. python3 -m scripts.team_teardown --team-pk <team_pk> [--team-id <team_id>]
      ```
      This enqueues exactly ONE `team_delete` bridge row (`status='pending'`,
      scoped to `<team_pk>`) and sets `teams.status='closed'` (the forward-safe
      hedge the sweep already honors). It is mode-gated: in non-local mode it
      WARNs and returns 0 without mutating (team-state mutators are Local-only).
      The CLI prints the enqueued `team_delete row id=<N>` to stderr.

   c. **Service the pending `team_delete` row** EXACTLY like
      `skills/abort/SKILL.md` step 4 — the bridge-poll servicer does NOT handle
      the `team_delete` lifecycle kind (`internal/bridge-poll/SKILL.md`'s
      closed-enum switch), so the completion SKILL services it itself. Call the
      harness `TeamDelete` tool for the team, then flip the enqueued row to
      `status='ready'`:
      ```sql
      UPDATE bridge_requests
      SET status = 'ready',
          completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
      WHERE id = :team_delete_row_id;
      ```
      Until you flip the row, `teams.status='closed'` already closes the sweep's
      over-report window via the hedge; flipping it promptly also lets filter (i)
      of `find_orphan_team_ids` subtract the team. Any cross-session team config
      directory left on disk is filesystem-only cleanup: `rm -rf
      ~/.claude/teams/<team_id>/`.

7. **Retro — surface phase bypasses:**

   ```python
   # Run as: python3 -c "<contents below>" (replace <project_id>)
   from collections import Counter
   from scripts import backend

   rows = backend.list_phase_bypasses(project_id=<project_id>)
   if not rows:
       print("No phase bypasses during this project's lifecycle.")
   else:
       counts = Counter((r["from_phase"], r["to_phase"]) for r in rows)
       for (from_phase, to_phase), n in counts.most_common():
           print(f"from {from_phase} → {to_phase}: {n} bypass(es)")
   ```

   If any bypasses exist, present the summary. Ask the user if any should be captured as lessons.

## Hard rules

- Never advance to `handoff:complete` before the integration action exits zero.
- Never execute merge or abandon without explicit user confirmation.
- Pre-flight is not skippable — all four checks must pass.
- CI check must show actual output, not be asserted from memory.
- `handoff:complete` is terminal — new work requires a new project.
- All three integration outcomes write a session record.

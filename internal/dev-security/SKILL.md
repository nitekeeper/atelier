---
description: Use when review is approved and before QA — checks for vulnerabilities, exposed secrets, and insecure patterns.
---

# dev:security

Security review. Checks the implementation for vulnerabilities, exposed secrets, and insecure patterns before QA.

> **Prerequisites**
> - Mode: Memex or Local (mode-symmetric — `workflow.py` + `session.py` dispatch via `backend.py`)
> - Required: `review:approved` phase reached
> - Required tables: `projects`, `skill_gates`, `phase_bypasses`, `sessions` — seeded by Atelier bootstrap

## Hard gate

Requires `review:approved`.

## Procedure

1. Check the phase gate:
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:security
   ```
   Parse the JSON output: `{"allowed": bool, "current_phase": str, "required_phase": str, "reason": str}`.

   **If `allowed` is `true`**: record `current_phase` and proceed to the next step.

   **If `allowed` is `false`** (soft wall): ask the user:

   > *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*

   - On **yes**: run:
     ```
     python3 atelier/scripts/workflow.py <db_path> log-bypass <project_id> dev:security <current_phase> <required_phase>
     ```
     Optionally append `--agent <agent_id>` and `--note "<reason>"`. Then proceed to the next step.
   - On **no**: stop. Tell the user:
     > *"Advance to `<required_phase>` first (run `python3 atelier/scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*

2. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> security:open`

3. **Read prior session for carry-over security notes:**
   ```
   python3 atelier/scripts/session.py read-latest <project_id>
   ```
   If the returned `pm_notes` field references security-relevant concerns (carry-over debt, deferred audits, "next session must..."), include those items in the checklist evaluation below. If no prior session exists or `pm_notes` is empty, proceed to step 4.

4. Security checklist (all required):

   | # | Check |
   |---|---|
   | 1 | No secrets, API keys, or credentials in source or test files |
   | 2 | No hardcoded internal paths, hostnames, or IPs |
   | 3 | All external input is validated before use |
   | 4 | SQL queries use parameterised statements — no string interpolation |
   | 5 | File paths derived from user input are sanitised (no path traversal) |
   | 6 | Dependencies are pinned — no floating version constraints. Check `requirements.txt` or `pyproject.toml`: no `>=`, `~=`, or unpinned entries |
   | 7 | Error messages do not leak internal state to external callers |
   | 8 | Authentication and authorisation are not bypassable by changing a parameter |

5. **If issues are found:**
   - Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> security:changes-requested`
   - List each issue: file, line range, vulnerability class, recommended fix.
   - The engineer addresses all issues.
   - On re-review: advance back to `security:open` first:
     ```
     python3 atelier/scripts/workflow.py <db_path> advance <project_id> security:open
     ```
     If the advance command exits non-zero (e.g., `WorkflowError: invalid transition`), do NOT silently continue. Surface the error to the user, run `python3 atelier/scripts/workflow.py <db_path> show <project_id>` to capture the project's actual current phase, and ask: "Re-review advance failed. Project is in phase: <current>. How should I proceed?" Wait for the user's direction before retrying.
     Then repeat the checklist from step 4.

6. **If no issues found:**
   - Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> security:approved`
   - Confirm: "Security review approved. Phase: security:approved. Ready for dev:qa."

## Hard rules
- Run `pytest -v` before approving — security fixes must not regress tests.
- Never mark an item as passed without explicitly checking it — no assumed passes.
- Secrets found anywhere in source are a hard block — no exceptions.

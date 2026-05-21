---
description: Use when implementation is complete — examines code for correctness, maintainability, and spec compliance.
---

# dev:review

Code review. Examines the implementation for correctness, maintainability, and spec compliance. Produces a review decision.

## Hard gate

Requires `tdd:clean`.

## Procedure

1. Check the phase gate:
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:review
   ```
   Parse the JSON output: `{"allowed": bool, "current_phase": str, "required_phase": str, "reason": str}`.

   **If `allowed` is `true`**: record `current_phase` and proceed to the next step.

   **If `allowed` is `false`** (soft wall): ask the user:

   > *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*

   - On **yes**: run:
     ```
     python3 atelier/scripts/workflow.py <db_path> log-bypass <project_id> dev:review <current_phase> <required_phase>
     ```
     Optionally append `--agent <agent_id>` and `--note "<reason>"`. Then proceed to the next step.
   - On **no**: stop. Tell the user:
     > *"Advance to `<required_phase>` first (run `python3 atelier/scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*

2. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> review:open`

3. Read the plan and design documents:
   ```
   python3 atelier/scripts/documents.py list --project_id <project_id>
   ```
   This returns JSON with all documents. Extract the `filename` fields for both the plan and design documents. Read both files — the review must check against both for spec compliance and implementation quality.

4. Review checklist (all required):

   | # | Check |
   |---|---|
   | 1 | All plan tasks implemented |
   | 2 | All tests pass (`pytest -v`) |
   | 3 | No dead code or leftover stubs |
   | 4 | Public interfaces match the design doc |
   | 5 | Error handling is explicit (no silent swallowing) |
   | 6 | No hardcoded paths, secrets, or environment assumptions |
   | 7 | Naming is clear — functions say what they do |
   | 8 | No duplication that belongs in a shared helper |

5. **If changes are required:**
   - Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> review:changes-requested`
   - List each issue precisely: file, line range, what is wrong, what the fix should be.
   - The engineer fixes the issues, re-runs tests, and re-requests review.
   - On re-review (the REVIEWER runs this): advance back to `review:open` first:
     ```
     python3 atelier/scripts/workflow.py <db_path> advance <project_id> review:open
     ```
     Then repeat the review checklist.

6. **If approved:**
   - Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> review:approved`
   - Confirm: "Code review approved. Phase: review:approved. Ready for dev:security."

## Hard rules
- Run `pytest -v` before approving — never approve with failing tests.
- Issues must be specific (file + line range + fix direction) — vague comments are not actionable.
- Do not approve partial fixes — all issues from the previous review must be resolved before re-approving.

# dev:review

Code review. Examines the implementation for correctness, maintainability, and spec compliance. Produces a review decision.

## Hard gate

Requires `tdd:clean`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:review`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> review:open`

3. Read the plan and design documents:
   ```
   python atelier/scripts/documents.py list <project_id>
   ```
   Open both. The review must check against both — spec compliance and implementation quality.

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
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> review:changes-requested`
   - List each issue precisely: file, line range, what is wrong, what the fix should be.
   - The engineer fixes the issues, re-runs tests, and re-requests review.
   - On re-review: advance back to `review:open` first:
     ```
     python atelier/scripts/workflow.py advance <project_id> review:open
     ```
     Then repeat the review checklist.

6. **If approved:**
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> review:approved`
   - Confirm: "Code review approved. Phase: review:approved. Ready for dev:security."

## Hard rules
- Run `pytest -v` before approving — never approve with failing tests.
- Issues must be specific (file + line range + fix direction) — vague comments are not actionable.
- Do not approve partial fixes — all issues from the previous review must be resolved before re-approving.

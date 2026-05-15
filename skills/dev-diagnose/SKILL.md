# dev:diagnose

Bug diagnosis. Can be entered from any phase. Identifies root cause, writes a regression test, fixes the root cause, and resumes the interrupted phase.

## Hard gate

None — callable from any phase.

## Procedure

1. Check the phase gate:
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:diagnose
   ```
   Parse the JSON output `{"allowed": bool, "current_phase": str, "required_phase": str|null, "reason": str}`.
   For this skill `allowed` is always `true` (no gate configured). Record `current_phase` for later use, then proceed to the next step.
   - If the project does not exist, stop and tell the user to create one first with `project:create`.

   *Note: the `current_phase` from check-gate is recorded as `<pre_diagnose_phase>` for restoration on resolve (step 13).*

2. Write a session entry to record the diagnose entry and save the interrupted phase:
   ```
   python atelier/scripts/session.py write <project_id> <agent_id> diagnose:open in-progress \
     --pre-diagnose-phase <pre_diagnose_phase> \
     --notes "Entering diagnose: <one-line bug description>"
   ```

3. Advance phase:
   ```
   python atelier/scripts/workflow.py advance <project_id> diagnose:open
   ```

4. Determine if the bug is reproducible deterministically.
   - If not reproducible: stop. Gather more information before proceeding. Do not guess at root cause.

5. Write a regression test that captures the failure **before** fixing:
   - The test must fail before the fix and pass after.
   - Name it `test_regression_<short-description>`.

6. Identify the affected layer:
   - Design error → after fix, restore to `design:open`
   - Implementation error → fix in current branch, restore to `<pre_diagnose_phase>`
   - Review miss → write a session note via `python atelier/scripts/session.py write <project_id> <agent_id> <pre_diagnose_phase> in-progress --notes "Review miss: <what was missed>"`, then restore to `<pre_diagnose_phase>`

7. Fix the root cause. Not the symptom.

8. Run the regression test:
   ```
   pytest <test-path>::test_regression_<name> -v
   ```
   Expected: PASS

9. Run the full suite:
   ```
   pytest -v
   ```
   Expected: all tests pass including the regression.

10. Commit:
    ```
    git add <test-file> <fix-file>
    git commit -m "fix: <root cause description> (regression test included)"
    ```

11. Advance to resolved:
    ```
    python atelier/scripts/workflow.py advance <project_id> diagnose:resolved
    ```

12. Read the latest session to retrieve the pre_diagnose_phase:
    ```
    python atelier/scripts/session.py read-latest <project_id>
    ```
    Extract the `pre_diagnose_phase` field from the output.

13. Restore the project to the interrupted phase:
    ```
    python atelier/scripts/workflow.py force-phase <project_id> <pre_diagnose_phase>
    ```
    Confirm: "Bug resolved. Regression test added. Restored to <pre_diagnose_phase>. Ready to resume."

## Hard rules
- Write the regression test before the fix — always.
- Fix root cause, not symptom.
- Never proceed on a non-deterministically reproducible bug — gather more information first.
- Always restore the project to the pre-diagnose phase on resolution.

# dev:diagnose

Bug diagnosis. Can be entered from any phase. Identifies root cause, writes a regression test, fixes the root cause, and resumes the interrupted phase.

## Hard gate

None — callable from any phase.

## Procedure

1. Record in `.ai/work.md`:
   - Bug description
   - Reproduction steps (exact commands or inputs)
   - Observed behaviour vs. expected behaviour

2. Determine if the bug is reproducible deterministically.
   - If not reproducible: stop. Gather more information before proceeding. Do not guess at root cause.

3. Identify the affected phase:
   - Design error → return project to `design:in-progress` via: `python atelier/scripts/workflow.py force-phase <project_id> design:in-progress`
   - Implementation error → fix in current branch, re-run tests
   - Review miss → document what was missed in the review checklist

4. Write a regression test that captures the failure **before** fixing:
   - The test must fail before the fix and pass after.
   - Name it `test_regression_<short-description>`.

5. Fix the root cause. Not the symptom.

6. Run the regression test:
   ```
   pytest <test-path>::test_regression_<name> -v
   ```
   Expected: PASS

7. Run the full suite:
   ```
   pytest -v
   ```
   Expected: all tests pass including the regression.

8. Commit:
   ```
   git add <test-file> <fix-file>
   git commit -m "fix: <root cause description> (regression test included)"
   ```

9. Resume the interrupted phase.

## Hard rules
- Write the regression test before the fix — always.
- Fix root cause, not symptom.
- Never proceed on a non-deterministically reproducible bug — gather more information first.

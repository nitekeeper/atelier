# dev:tdd-refactor

Improves code structure without changing behaviour. Tests must still pass after every change.

## Hard gate

Requires `tdd:green`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:tdd-refactor`
   If the gate fails, state the current phase and stop.

2. Advance project phase: `python atelier/scripts/workflow.py advance <project_id> tdd:refactor`

3. For each file touched during tdd:green, apply the refactor step:
   a. Identify duplication, unclear names, oversized functions, or mixed responsibilities.
   b. Make one targeted improvement at a time.
   c. After each change, run the full suite:
      ```
      pytest -v
      ```
      Expected: all tests still pass. If any test fails, revert the change immediately.

4. Commit the refactored code separately from the implementation commits:
   ```
   git add <files>
   git commit -m "refactor: <description of structural improvement>"
   ```

5. Confirm: "Refactor complete. All tests still passing. Phase is tdd:refactor. Ready for `dev:code-review`."

## Hard rules
- Refactoring commits must be separate from implementation commits. No mixing.
- If any test fails after a refactoring change, revert immediately — do not try to fix it.
- No new features during refactoring. Behaviour must be identical before and after.

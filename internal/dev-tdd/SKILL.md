---
description: Use when implementing a plan task — drives red/green/clean TDD cycles one task at a time.
---

# dev:tdd

Test-driven development. Implements plan tasks one at a time using red → green → clean cycles. The primary implementation skill.

> **Prerequisites**
> - Mode: Memex or Local (mode-symmetric — `workflow.py` + `documents.py` dispatch via `backend.py`)
> - Required: `plan:approved` phase reached; plan document readable via `documents.py list`
> - Required tables: `projects`, `skill_gates`, `phase_bypasses`, `project_documents` — seeded by Atelier bootstrap

## Hard gate

Requires `plan:approved`.

## The Iron Law

No production code without a failing test first. No exceptions.

If you wrote implementation before the test: delete it. Not "keep as reference." Not "adapt while writing the test." Delete means delete — then write the test, watch it fail, then implement from scratch.

"Simple change," "obvious fix," and "just this once" are rationalizations, not reasons. The order is the method. Violating the order means you are not doing TDD.

## Procedure

1. Check the phase gate:
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:tdd
   ```
   Parse the JSON output: `{"allowed": bool, "current_phase": str, "required_phase": str, "reason": str}`.

   **If `allowed` is `true`**: record `current_phase` and proceed to the next step.

   **If `allowed` is `false`** (soft wall): ask the user:

   > *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*

   - On **yes**: run:
     ```
     python3 atelier/scripts/workflow.py <db_path> log-bypass <project_id> dev:tdd <current_phase> <required_phase>
     ```
     Optionally append `--agent <agent_id>` and `--note "<reason>"`. Then proceed to the next step.
   - On **no**: stop. Tell the user:
     > *"Advance to `<required_phase>` first (run `python3 atelier/scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*

2. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:red`

3. Read the plan document:
   ```
   python3 atelier/scripts/documents.py list --project_id <project_id>
   ```
   Open the plan. Work one task at a time in order.

### Red cycle

4. Write the failing test for the current task.
   - Test name must match the plan.
   - Do not implement anything yet — only the test.

5. Run the test and confirm it fails:
   ```
   pytest <test-file>::<test-name> -v
   ```
   Expected: FAIL. If it passes without implementation, the test is wrong — rewrite it.

### Green cycle

6. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:green`

7. Write the minimal implementation to make the test pass.
   - Minimal means: the simplest code that passes the test, no more.
   - Do not add features, logging, or "nice-to-haves" not required by the test.

8. Run the test:
   ```
   pytest <test-file>::<test-name> -v
   ```
   Expected: PASS. If it fails, fix the implementation. Repeat until green.

9. Run the full test suite:
   ```
   pytest -q --tb=short
   ```
   Expected: all tests pass. If any test regressed, fix it before continuing.

### Clean cycle

10. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:clean`

11. Refactor the implementation:
    - Remove duplication.
    - Apply naming that communicates intent.
    - Extract helpers if the function is doing more than one thing.
    - Do NOT add new behaviour during refactor.

12. Run the full test suite again:
    ```
    pytest -q --tb=short
    ```
    Expected: all tests still pass. If any test fails after refactor, the refactor changed behaviour — revert and refactor more carefully.

13. Commit:
    ```
    git add <test-file> <implementation-file>
    git commit -m "test+feat: <task title from plan>"
    ```

### Repeat or advance

14. If more tasks remain in the plan:
    - Advance phase back to red: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:red`
    - Return to step 4 for the next task.

15. When all plan tasks are complete:
    - Confirm: "All plan tasks complete. Phase: tdd:clean. Next phase: review:open. Invoke dev:review to begin."
    - Do not advance to review — the PM or engineer initiates dev:review.

## Hard rules
- Write the test before the implementation — always.
- Confirm the test fails before implementing.
- Run the full suite after every green cycle and after every clean cycle.
- Never commit with failing tests.
- Never add functionality not required by the current test.

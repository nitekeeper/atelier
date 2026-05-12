# dev:tdd-red

Writes failing tests for the current project. Tests are written before any implementation code. Tasks are created from the test scenarios.

## Hard gate

Requires `plan:approved`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:tdd-red`
   If the gate fails, state the current phase and stop.

2. Read the implementation plan document.

3. Advance project phase: `python atelier/scripts/workflow.py advance <project_id> tdd:red`

4. For each task in the plan, apply the TDD red step:
   a. List all test scenarios for this task — enumerate before writing any code.
   b. Write one failing test per scenario. Each test must:
      - Fail for the right reason (not a syntax error or import error)
      - Have specific assertions — not just "no exception raised"
      - Test behaviour, not structure

5. Run the full test suite. Confirm all new tests fail, no existing tests break.
   ```
   pytest <test-path> -v
   ```

6. For each test scenario, create a task:
   `python atelier/scripts/tasks.py create <project_id> "<task-title>" "<agent_id>" --description "<scenario>"`

7. Confirm: "Tests written and failing. [N] tasks created. Phase is tdd:red. Ready for task assignment and `dev:tdd-green`."

## Hard rules
- No implementation code in this phase. If any implementation code is written, stop and remove it.
- Tests must actually fail — run them and confirm before proceeding.
- Tasks are created from test scenarios, not from the plan directly.

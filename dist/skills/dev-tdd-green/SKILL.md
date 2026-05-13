# dev:tdd-green

Implements code until all failing tests pass. No new features beyond what the tests require.

## Hard gate

Requires tasks to be assigned (at least one task in `in-progress` status for this project). The coordinator must have assigned tasks before this phase begins.

## Procedure

1. Check tasks are assigned:
   `python atelier/scripts/tasks.py list --project_id <project_id> --status assigned`
   If no assigned tasks exist, stop: "Tasks must be assigned by the coordinator before implementation begins."

2. Advance project phase: `python atelier/scripts/workflow.py advance <project_id> tdd:green`

3. For each assigned task, apply the TDD green step:
   a. Claim the task: `python atelier/scripts/tasks.py claim <task_id> <agent_id>`
   b. Write the minimal code to make the failing test pass — no more.
   c. Run only the test for this scenario:
      ```
      pytest <test-path>::<test-name> -v
      ```
      Expected: PASS
   d. Run the full suite:
      ```
      pytest -v
      ```
      Expected: no regressions
   e. Complete the task: `python atelier/scripts/tasks.py complete <task_id>`

4. Confirm: "All tests passing. No regressions. Phase is tdd:green. Ready for `dev:tdd-refactor`."

## Hard rules
- Write only enough code to make the failing test pass. No speculative code.
- Run the full suite after each task — never leave regressions unresolved.
- Do not mark a task complete until its test passes and the full suite is green.

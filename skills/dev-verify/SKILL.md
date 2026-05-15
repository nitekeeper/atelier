---
name: dev:verify
description: Use when about to claim any work is complete, fixed, or passing — runs the five-step gate before any success assertion.
---

# dev:verify

Universal completion check. Before claiming any status — "tests pass", "feature works", "bug fixed", "PR ready" — identify the command that proves it, run it fresh, and read the full output. A confident mental model is not evidence. Confidence is the precondition for the most dangerous skips.

## Hard gate

None — callable from any phase.

## When to use

- Before saying "done", "fixed", "passing", or "ready"
- Before committing or opening a PR
- Before marking a task complete
- After any change that could affect a system you are about to describe as working

## Procedure

1. **Identify** — state the exact command that proves the claim. If no such command exists, the claim is not verifiable; stop until one does.

2. **Run** — execute the command now, in the current environment. Prior runs do not count. Quote the relevant output lines — do not paraphrase from memory.

3. **Read** — read the full output. Not a summary. Not the last line. If the suite ran 50 tests and one failed on line 40 of the output, you must see it.

4. **Vacuity check** — if the claim involves a test passing: temporarily break the implementation (comment out the core line) and re-run the targeted test. Expected result: FAIL. If the test still passes with a broken implementation, the test does not cover the behaviour — fix the test, restore the implementation, and restart from step 1.

5. **Claim** — only now state the status. Write one sentence per step with the actual result before declaring done. Example: "Suite: 47 passed, 0 failed. Diff: 3 lines changed, no debug code. Vacuity check: test failed on broken impl."

## Common failures

| Claim | What verification requires | Common shortcut that fails |
|---|---|---|
| "Tests pass" | Full suite output, 0 failures, current run | "Last run was green" / "should pass" |
| "Bug is fixed" | Regression test passing in current state | Fix merged, suite not re-run |
| "No regressions" | Full suite output, all tests green | Ran only the new test |
| "Feature works" | Observed output of the feature under test | Read the code and assumed |
| "PR is ready" | CI green + review checklist complete | Local suite only, CI not checked |
| "Migration applied" | DB schema query confirming the change exists | Script ran without error |
| "File was written" | Read the file and confirm its contents | Write tool reported success |
| "Dependency installed" | Import or version check in the target environment | Installer exited 0 |
| "No regressions (subset)" | Full suite, not just the changed module's tests | Only ran `pytest tests/test_foo.py` |

## Red flags

These phrases are known signals that the gate was not completed:

- "should pass", "probably works", "seems to be fine"
- "I didn't re-run but…"
- "the previous output showed…"
- Expressing satisfaction or confidence before quoting command output
- Completing step 5 without having run steps 1–4 in this session

## Hard rules

- Run the verification command in the current environment — not a recalled result.
- Quote the relevant output lines directly; do not paraphrase from memory.
- The vacuity check (step 4) is not optional when the claim involves test coverage.
- If any step produces unexpected output: stop. Do not rationalize past it. Re-enter `dev:diagnose`.
- Never declare work complete without completing all five steps in order.

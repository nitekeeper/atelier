# dev:code-review

Reviews the implementation against the design document. Produces a code review report. Blocks on unresolved issues.

## Hard gate

Requires `tdd:refactor`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:code-review`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> code-review:draft`

3. Count the diff LOC:
   ```
   git diff main...HEAD --stat
   ```
   If diff exceeds 400 LOC: stop. Ask the human to split the PR before review begins.

4. **Step 1 — Broad assessment:** Read the PR description and design document.
   - Does this change match what was agreed in the design phase?
   - If conceptually wrong (wrong scope, wrong approach): stop. Return project to design phase.

5. **Step 2 — Main files first:** Review files with the largest logical changes for design-level concerns.
   - If major design problems found: state them now before reviewing other files.

6. **Step 3 — Tests first, then implementation:**
   - Read test files before implementation files.
   - Verify tests would actually fail if the underlying code broke.
   - Check: are tests present? If not, return to `dev:tdd-red`.

7. **Review checklist** (blocking unless marked Nit):

   | # | Dimension | Blocking? |
   |---|---|---|
   | 1 | Design — matches approved design doc? | Yes |
   | 2 | Functionality — does it work? Edge cases? | Yes |
   | 3 | Complexity — understandable without explanation? | Yes |
   | 4 | Tests — present, meaningful, would fail? | Yes |
   | 5 | Naming — communicates purpose? | Yes |
   | 6 | Comments — explain why, not what? | Yes |
   | 7 | Security — injection, auth, secrets, exposure? | Yes |
   | 8 | Style — only what linting can't catch | Nit |
   | 9 | Consistency — matches existing patterns? | Nit |
   | 10 | Documentation — READMEs updated? | Yes if applicable |
   | + | Rollback safety — migrations backwards-compatible? | Yes if applicable |

8. If changes required:
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> code-review:changes-requested`
   - State all blocking issues clearly.
   - When resolved: `python atelier/scripts/workflow.py advance <project_id> code-review:draft`
   - Re-review from Step 3.

9. When approved:
   - Write the code review report to `docs/reports/<project-slug>-code-review.md`
   - Register: `python atelier/scripts/documents.py create <project_id> code-review-report "<title>" "<filename>" "<agent_id>"`
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> code-review:merged`
   - Confirm: "Code review complete. Report saved. Ready for `dev:security-review`."

## Hard rules
- Comments are about code properties, not the developer. Never personal.
- "Clean it up later" is refused. Cleanup happens before merge, except declared emergencies logged in `.ai/work.md` under `cleanup-debt`.
- If tests are absent, return to `dev:tdd-red`. Do not review untested code.

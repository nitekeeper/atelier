# Atelier Dev-Skill Integration — Plan 2: Skills, Hook & Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update all dev:* SKILL.md files to use the unified 19-phase vocabulary, create the session-open hook backed by the DB, and remove the obsolete dev-skill artefacts.

**Architecture:** Plan 1 (Foundation) must be fully applied before starting Plan 2 — migrations 002–004 must be applied and session.py / workflow.py must be the DB-backed rewrites. Plan 2 is pure content: skill directory renames, SKILL.md rewrites, a new hook, and cleanup.

**Tech Stack:** Python 3.12, SQLite (via session.py), git mv for directory renames, gh CLI for repo deletion.

**Spec:** `docs/superpowers/specs/2026-05-13-atelier-dev-skill-integration.md`

---

## File Structure

| Action | Path |
|---|---|
| git mv | `skills/dev-code-review/` → `skills/dev-review/` |
| git mv | `skills/dev-security-review/` → `skills/dev-security/` |
| git mv | `skills/dev-qa-review/` → `skills/dev-qa/` |
| Delete dirs | `skills/dev-tdd-red/`, `skills/dev-tdd-green/`, `skills/dev-tdd-refactor/` |
| Create dir | `skills/dev-tdd/` |
| Modify | `skills/dev-plan/SKILL.md` |
| Create | `skills/dev-tdd/SKILL.md` |
| Modify | `skills/dev-review/SKILL.md` (after rename) |
| Modify | `skills/dev-security/SKILL.md` (after rename) |
| Modify | `skills/dev-qa/SKILL.md` (after rename) |
| Modify | `skills/dev-diagnose/SKILL.md` |
| Modify | `skills/dev-handoff/SKILL.md` |
| Create | `hooks/session_open.py` |
| Create | `tests/test_session_open_hook.py` |
| Modify | `C:\Users\user\Documents\Skills\skill-atelier\ROADMAP.md` |

**Note:** `skills/dev-design/SKILL.md` requires no changes — it already uses correct phase names and workflow.py calls.

---

### Task 1: Rename skill directories

**Files:**
- git mv: `skills/dev-code-review` → `skills/dev-review`
- git mv: `skills/dev-security-review` → `skills/dev-security`
- git mv: `skills/dev-qa-review` → `skills/dev-qa`
- Remove: `skills/dev-tdd-red/`, `skills/dev-tdd-green/`, `skills/dev-tdd-refactor/`
- Create (empty): `skills/dev-tdd/`

There are no automated tests for directory renames. Verification is by inspection.

- [ ] **Step 1: Rename the three review-type skills**

```bash
cd C:\Users\user\Documents\Skills\atelier
git mv skills/dev-code-review skills/dev-review
git mv skills/dev-security-review skills/dev-security
git mv skills/dev-qa-review skills/dev-qa
```

- [ ] **Step 2: Verify renames**

```bash
ls skills/
```

Expected: `dev-design`, `dev-diagnose`, `dev-handoff`, `dev-plan`, `dev-qa`, `dev-review`, `dev-security`, plus everything else. No `dev-code-review`, `dev-security-review`, `dev-qa-review`.

- [ ] **Step 3: Remove the three split TDD sub-skill directories**

```bash
git rm -r skills/dev-tdd-red skills/dev-tdd-green skills/dev-tdd-refactor
```

- [ ] **Step 4: Create the unified dev-tdd directory**

```bash
mkdir skills/dev-tdd
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: rename dev skill dirs to unified vocabulary

dev-code-review → dev-review
dev-security-review → dev-security
dev-qa-review → dev-qa
Remove dev-tdd-red, dev-tdd-green, dev-tdd-refactor (replaced by dev-tdd in next commit)"
```

---

### Task 2: Update dev:plan SKILL.md

**Files:**
- Modify: `skills/dev-plan/SKILL.md`

Change: `plan:in-progress` → `plan:open`, reference to `dev:tdd-red` → `dev:tdd`.

- [ ] **Step 1: Write the updated SKILL.md**

Write this complete content to `skills/dev-plan/SKILL.md`:

```markdown
# dev:plan

Produces the implementation plan from the approved design document. Breaks the design into ordered tasks. Identifies the vertical slice.

## Hard gate

Requires `design:approved`. The skill refuses if the project is not at this phase.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:plan`
   If the gate fails, state the current phase and stop.

2. Retrieve the design document: `python atelier/scripts/documents.py list --project_id <project_id> --type design`
   Read the design document file.

3. Advance project to plan phase: `python atelier/scripts/workflow.py advance <project_id> plan:open`

4. Decompose the design into an ordered task list. Each task must be:
   - Completable in a single coding session
   - Independently reviewable
   - Described as a verb phrase: "Add X", "Refactor Y", "Remove Z"
   - Refactoring tasks separated from feature tasks (never mixed in the same task)

5. Identify the **vertical slice**: the minimum set of tasks that produces an end-to-end observable result. Mark it explicitly.

6. If the task list exceeds 10 items: flag this to the human before proceeding. The design scope may be too large.

7. Present the plan to the human for review. Ask: "Does this plan match the design? What should change?"
   Revise until the human explicitly approves.

8. Write the implementation plan to a file (e.g. `docs/plans/<project-slug>-plan.md`).

9. Register the document: `python atelier/scripts/documents.py create <project_id> implementation-plan "<title>" "<filename>" "<agent_id>"`

10. Advance phase: `python atelier/scripts/workflow.py advance <project_id> plan:approved`

11. Confirm: "Implementation plan approved. Phase advanced to plan:approved. Ready for `dev:tdd`."

## Hard rules
- Refactoring and feature implementation are separate tasks. Never mix them.
- Never advance the phase without explicit human approval.
- If >10 tasks: stop and flag to the human before proceeding.
```

- [ ] **Step 2: Inspect — verify no old phase names remain**

```bash
grep -n "plan:in-progress\|tdd-red\|tdd-green\|tdd-refactor" skills/dev-plan/SKILL.md
```

Expected: no matches.

- [ ] **Step 3: Commit**

```bash
git add skills/dev-plan/SKILL.md
git commit -m "fix(skill): dev:plan — plan:in-progress → plan:open, tdd-red ref → dev:tdd"
```

---

### Task 3: Create dev:tdd SKILL.md (unified TDD skill)

**Files:**
- Create: `skills/dev-tdd/SKILL.md`

Merges the three former sub-skills (dev-tdd-red, dev-tdd-green, dev-tdd-refactor) into one unified skill covering all three phases: red, green, clean.

- [ ] **Step 1: Write the unified SKILL.md**

Write this complete content to `skills/dev-tdd/SKILL.md`:

```markdown
# dev:tdd — TDD Phase

**Announce at start:** "I'm using dev:tdd to run the TDD phase."

## Hard gate

Requires `plan:approved`.

## Overview

The TDD phase cycles through three sub-phases for each task in the implementation plan:
- **Red**: write a failing test
- **Green**: write minimal code to pass the test
- **Clean**: refactor without breaking tests

Repeat for each task. When all tasks are done and clean, advance to `review:open`.

## Entry

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:tdd`
   If the gate fails, state the current phase and stop.

2. Read the implementation plan document from `docs/plans/`.

3. Advance to red: `python atelier/scripts/workflow.py advance <project_id> tdd:red`

## Red — write failing tests

For each task in the plan:

1. List all test scenarios for this task before writing any code.

2. Write one failing test per scenario:
   - Must fail for the right reason (not import or syntax error)
   - Must have specific assertions — not just "no exception raised"
   - Tests behaviour, not structure
   - Named `test_<scenario_description>`

3. Run the tests: `pytest <test-path> -v`
   Confirm all new tests fail; confirm no existing tests break.

4. Create a task for each test scenario:
   ```
   python atelier/scripts/tasks.py create <project_id> "<task-title>" "<agent_id>" --description "<scenario>"
   ```

After all tasks have failing tests, advance to green:
```
python atelier/scripts/workflow.py advance <project_id> tdd:green
```

**Hard rules — Red:**
- No implementation code in the red sub-phase. If any is written, stop and remove it.
- Tests must actually fail — run them and confirm before advancing.

## Green — make tests pass

For each task:

1. Claim the task:
   ```
   python atelier/scripts/tasks.py claim <task_id> <agent_id>
   ```

2. Write the minimal code to make the failing test pass — no more.
   No speculative code. No code not required by a test.

3. Run the specific test: `pytest <test-path>::<test-name> -v`
   Expected: PASS

4. Run the full suite: `pytest -v`
   Expected: no regressions. Fix any regression before proceeding.

5. Complete the task:
   ```
   python atelier/scripts/tasks.py complete <task_id>
   ```

After all tasks pass and the full suite is green, advance to clean:
```
python atelier/scripts/workflow.py advance <project_id> tdd:clean
```

**Hard rules — Green:**
- Write only enough code to make the failing test pass.
- Run the full suite after each task — never leave regressions.
- Do not mark a task complete until its test passes and the full suite is green.

## Clean — refactor

For each file touched during the green sub-phase:

1. Identify duplication, unclear names, oversized functions, or mixed responsibilities.
2. Make one targeted improvement at a time.
3. After each change, run the full suite:
   ```
   pytest -v
   ```
   Expected: all tests still pass. If any test fails, revert the change immediately.

4. Commit the refactored code separately from implementation commits:
   ```
   git add <files>
   git commit -m "refactor: <description>"
   ```

**After the clean sub-phase:**
- If more tasks remain in the plan → start the next task cycle at red:
  ```
  python atelier/scripts/workflow.py advance <project_id> tdd:red
  ```
- If all tasks are done and the suite is fully green → advance to review:
  ```
  python atelier/scripts/workflow.py advance <project_id> review:open
  ```
  Confirm: "TDD complete. All tasks done, all tests passing, code refactored. Ready for `dev:review`."

**Hard rules — Clean:**
- Refactoring commits are separate from implementation commits. Never mix.
- If any test fails after a refactoring change, revert immediately — do not try to fix it.
- No new features during refactoring. Behaviour must be identical before and after.
```

- [ ] **Step 2: Inspect — confirm all three sub-phases present**

```bash
grep -n "## Red\|## Green\|## Clean\|tdd:red\|tdd:green\|tdd:clean\|review:open" skills/dev-tdd/SKILL.md
```

Expected: each sub-phase heading and each phase name appears at least once.

- [ ] **Step 3: Commit**

```bash
git add skills/dev-tdd/SKILL.md
git commit -m "feat(skill): dev:tdd — unified TDD skill (red/green/clean)"
```

---

### Task 4: Update dev:review SKILL.md

**Files:**
- Modify: `skills/dev-review/SKILL.md` (already renamed from dev-code-review in Task 1)

Phase changes: `tdd:refactor` → `tdd:clean`, `code-review:draft` → `review:open`,
`code-review:changes-requested` → `review:changes-requested`, `code-review:merged` →
`review:approved`. Remove `.ai/work.md` cleanup-debt reference. Update next-skill
reference: `dev:security-review` → `dev:security`. Update "return to" reference:
`dev:tdd-red` → `dev:tdd`.

- [ ] **Step 1: Write the updated SKILL.md**

Write this complete content to `skills/dev-review/SKILL.md`:

```markdown
# dev:review

Reviews the implementation against the design document. Produces a code review report. Blocks on unresolved issues.

## Hard gate

Requires `tdd:clean`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:review`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> review:open`

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
   - Check: are tests present? If not, return to `dev:tdd`.

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
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> review:changes-requested`
   - State all blocking issues clearly.
   - When resolved: `python atelier/scripts/workflow.py advance <project_id> review:open`
   - Re-review from Step 3.

9. When approved:
   - Write the code review report to `docs/reports/<project-slug>-review.md`
   - Register: `python atelier/scripts/documents.py create <project_id> review-report "<title>" "<filename>" "<agent_id>"`
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> review:approved`
   - Confirm: "Review complete. Report saved. Ready for `dev:security`."

## Hard rules
- Comments are about code properties, not the developer. Never personal.
- Cleanup happens before merge — no "we'll clean it up later."
- If tests are absent, return to `dev:tdd`. Do not review untested code.
```

- [ ] **Step 2: Inspect — confirm no old phase names remain**

```bash
grep -n "tdd:refactor\|code-review:\|dev:tdd-red\|dev:security-review\|work\.md" skills/dev-review/SKILL.md
```

Expected: no matches.

- [ ] **Step 3: Commit**

```bash
git add skills/dev-review/SKILL.md
git commit -m "fix(skill): dev:review — update to unified phase vocabulary"
```

---

### Task 5: Update dev:security SKILL.md

**Files:**
- Modify: `skills/dev-security/SKILL.md` (already renamed from dev-security-review in Task 1)

Phase changes: gate `code-review:merged` → `review:approved`, `security-review:in-progress`
→ `security:open`, `security-review:approved` → `security:approved`. Add the
changes-requested loop (missing from the original). Update next-skill reference:
`dev:qa-review` → `dev:qa`.

- [ ] **Step 1: Write the updated SKILL.md**

Write this complete content to `skills/dev-security/SKILL.md`:

```markdown
# dev:security

Reviews the implementation for security vulnerabilities. Produces a security report. Blocking issues must be resolved before advancing.

## Hard gate

Requires `review:approved`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:security`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> security:open`

3. **Threat model review** — re-read the Cross-cutting concerns section of the design document.
   Verify the implementation addresses all threats identified at design time.

4. **Security checklist** (all blocking):

   | Area | What to check |
   |---|---|
   | Injection | SQL injection, command injection, path traversal — are all inputs parameterised or sanitised? |
   | Authentication | Are auth checks present on all protected paths? Can they be bypassed? |
   | Authorisation | Does the code enforce who can do what, or just who is logged in? |
   | Secrets | Are secrets hardcoded, logged, or committed? |
   | Data exposure | Does the API return more data than the caller needs? |
   | Input validation | Is all input validated at system boundaries? |
   | Dependency vulnerabilities | Are all dependencies up to date? Run `pip-audit` or equivalent. |
   | Error messages | Do error messages leak implementation details? |

5. **Security test coverage** — verify tests exist for:
   - Authentication paths (valid + invalid credentials)
   - Authorisation (access denied cases)
   - Input validation (boundary values, injection attempts)

6. If blocking issues found:
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> security:changes-requested`
   - State all issues with file references and proposed fixes.
   - When resolved: `python atelier/scripts/workflow.py advance <project_id> security:open`
   - Re-review from Step 3.

7. When clean:
   - Write the security review report to `docs/reports/<project-slug>-security.md`
   - Register: `python atelier/scripts/documents.py create <project_id> security-report "<title>" "<filename>" "<agent_id>"`
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> security:approved`
   - Confirm: "Security review complete. No blocking issues. Report saved. Ready for `dev:qa`."

## Hard rules
- No "no security concerns" without a documented reason why.
- Secrets in code are always blocking — no exceptions.
- Do not advance the phase if any blocking issue is unresolved.
```

- [ ] **Step 2: Inspect — confirm no old phase names remain**

```bash
grep -n "security-review:\|code-review:merged\|dev:qa-review" skills/dev-security/SKILL.md
```

Expected: no matches.

- [ ] **Step 3: Commit**

```bash
git add skills/dev-security/SKILL.md
git commit -m "fix(skill): dev:security — update to unified phase vocabulary, add changes-requested loop"
```

---

### Task 6: Update dev:qa SKILL.md

**Files:**
- Modify: `skills/dev-qa/SKILL.md` (already renamed from dev-qa-review in Task 1)

Phase changes: gate `security-review:approved` → `security:approved`, `qa-review:in-progress`
→ `qa:open`, `qa-review:approved` → `qa:approved`. Remove all `.ai/work.md` references
(cleanup-debt and surfaced-issues now come from tasks and session notes, not WORK.md).
Update checklist items that referenced WORK.md.

- [ ] **Step 1: Write the updated SKILL.md**

Write this complete content to `skills/dev-qa/SKILL.md`:

```markdown
# dev:qa

Pre-deployment verification. Ensures all gates are clean, all debt is resolved, and all acceptance criteria are met. Produces a QA report.

## Hard gate

Requires `security:approved`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:qa`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> qa:open`

3. **Pre-deploy checklist** (all blocking):

   | # | Check | How to verify |
   |---|---|---|
   | 1 | CI pipeline green | All tests pass, linting clean, security scans pass |
   | 2 | All assigned tasks complete | `python atelier/scripts/tasks.py list --project_id <id> --status assigned` — must be empty |
   | 3 | No open blocking tasks | `python atelier/scripts/tasks.py list --project_id <id> --status open` — review any found |
   | 4 | Documentation updated | README, API docs, user-facing materials current |
   | 5 | Rollback plan exists | For migrations or serialisation changes — is rollback documented? |
   | 6 | Acceptance criteria met | Re-read design Goals section — is each goal demonstrably met? |

4. **Surfaced issues** — read pm_notes from the latest session:
   ```
   python atelier/scripts/session.py read-latest <project_id>
   ```
   For any issue flagged in `pm_notes`, ask the human: "Accept as known debt, or resolve now?"
   Require an explicit decision for each before proceeding.

5. If any checklist item fails: stop. State what is failing and what must be resolved before QA approval.

6. When all checks pass:
   - Write the QA report to `docs/reports/<project-slug>-qa.md`
   - Register: `python atelier/scripts/documents.py create <project_id> qa-report "<title>" "<filename>" "<agent_id>"`
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> qa:approved`
   - Confirm: "QA review approved. All checks passed. Project is ready for deployment. Phase: qa:approved."

## Hard rules
- Every checklist item must be explicitly verified — no assumed passes.
- Every surfaced issue requires an explicit human decision — do not auto-accept.
- Deployment is out of scope — `qa:approved` is the terminal state for Atelier dev workflow.
```

- [ ] **Step 2: Inspect — confirm no old phase names and no work.md references remain**

```bash
grep -n "security-review:\|qa-review:\|work\.md" skills/dev-qa/SKILL.md
```

Expected: no matches.

- [ ] **Step 3: Commit**

```bash
git add skills/dev-qa/SKILL.md
git commit -m "fix(skill): dev:qa — update phases, remove work.md refs, use session.py for surfaced issues"
```

---

### Task 7: Update dev:diagnose SKILL.md

**Files:**
- Modify: `skills/dev-diagnose/SKILL.md`

Phase changes: `diagnose:in-progress` → `diagnose:open`. Replace `.ai/work.md` recording
with session.py write (stores `pre_diagnose_phase`). Add explicit `diagnose:resolved` advance
step. Add `force-phase` restore step using `pre_diagnose_phase` from `session.py read-latest`.
Fix design-error phase reference: `design:in-progress` → `design:open`.

- [ ] **Step 1: Write the updated SKILL.md**

Write this complete content to `skills/dev-diagnose/SKILL.md`:

```markdown
# dev:diagnose

Bug diagnosis. Can be entered from any phase. Identifies root cause, writes a regression test, fixes the root cause, and resumes the interrupted phase.

## Hard gate

None — callable from any phase.

## Procedure

1. Get the current phase before entering diagnose:
   ```
   python atelier/scripts/workflow.py get-phase <project_id>
   ```
   Record this as `<pre_diagnose_phase>`.

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
   - Review miss → document what was missed, restore to `<pre_diagnose_phase>`

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
```

- [ ] **Step 2: Inspect — confirm no old phase names and no work.md references remain**

```bash
grep -n "diagnose:in-progress\|design:in-progress\|work\.md" skills/dev-diagnose/SKILL.md
```

Expected: no matches.

- [ ] **Step 3: Commit**

```bash
git add skills/dev-diagnose/SKILL.md
git commit -m "fix(skill): dev:diagnose — session.py integration, unified phase names, pre_diagnose_phase restore"
```

---

### Task 8: Update dev:handoff SKILL.md

**Files:**
- Modify: `skills/dev-handoff/SKILL.md`

Replace `session.py write .ai/work.md` (old flat-file CLI) with the new DB-backed
`session.py write <project_id> <agent_id> <phase> <status> [options]` CLI. Remove
all `.ai/work.md` references. Update "next-action" field name to `--next`.

- [ ] **Step 1: Write the updated SKILL.md**

Write this complete content to `skills/dev-handoff/SKILL.md`:

```markdown
# dev:handoff

Records current session state to the DB. Callable from any phase. Always the last action before closing a session.

## Hard gate

None — callable from any phase.

## Procedure

1. Determine current project state:
   ```
   python atelier/scripts/workflow.py get-phase <project_id>
   python atelier/scripts/tasks.py list --project_id <project_id>
   ```

2. Write session state:
   ```
   python atelier/scripts/session.py write <project_id> <agent_id> <current_phase> <status> \
     --accomplished "<what was completed this session>" \
     --next "<exact first action for the next session>" \
     [--notes "<pm notes for the next session>"] \
     [--blocking-reason "<what is blocking, if status is blocked>"]
   ```

   Where:
   - `<current_phase>`: result of `workflow.py get-phase`
   - `<status>`: `in-progress`, `blocked`, or `complete`
   - `--next`: specific imperative sentence naming the exact action (e.g. "Run `dev:tdd` for project 3")
   - `--blocking-reason` is required when `<status>` is `blocked`

3. Confirm: "Session state recorded. Next action: [next action]."

4. Ask: "Anything to capture to the knowledge base before closing? (y/n)"
   If yes: invoke `ingest`.

## Hard rules
- `--next` must be a specific imperative sentence — not "continue" or "resume".
- Always run dev:handoff before ending any session on a project.
- Status `blocked` requires `--blocking-reason` to be set.
```

- [ ] **Step 2: Inspect — confirm no old CLI signature or work.md references remain**

```bash
grep -n "work\.md\|current-task\|next-action\|blocking-reason:" skills/dev-handoff/SKILL.md
```

Expected: no matches for `work.md`, `current-task`. The `--blocking-reason` option may appear (that's correct).

- [ ] **Step 3: Commit**

```bash
git add skills/dev-handoff/SKILL.md
git commit -m "fix(skill): dev:handoff — replace flat-file session write with DB-backed session.py CLI"
```

---

### Task 9: Create hooks/session_open.py and tests

**Files:**
- Create: `hooks/session_open.py`
- Create: `tests/test_session_open_hook.py`

The hook reads the active project from `.ai/active_project`, calls
`session.py read-latest <project_id>`, and announces the session state to Claude context.
Errors never block a session (Option B from the spec).

- [ ] **Step 1: Write the failing tests**

Write this complete content to `tests/test_session_open_hook.py`:

```python
"""Tests for hooks/session_open.py"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add hooks dir to path for direct import
HOOKS_DIR = Path(__file__).parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import session_open  # noqa: E402


class TestFindActiveProject:
    def test_no_ai_dir(self, tmp_path):
        """No .ai/ directory → None."""
        assert session_open.find_active_project(tmp_path) is None

    def test_no_active_project_file(self, tmp_path):
        """Directory exists but no active_project file → None."""
        (tmp_path / ".ai").mkdir()
        assert session_open.find_active_project(tmp_path) is None

    def test_empty_file(self, tmp_path):
        """Empty file → None."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("")
        assert session_open.find_active_project(tmp_path) is None

    def test_whitespace_only(self, tmp_path):
        """Whitespace-only file → None."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("  \n  ")
        assert session_open.find_active_project(tmp_path) is None

    def test_valid_integer_id(self, tmp_path):
        """Valid project id → returned as string."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("42\n")
        assert session_open.find_active_project(tmp_path) == "42"

    def test_strips_whitespace(self, tmp_path):
        """ID with surrounding whitespace → stripped."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("  7  \n")
        assert session_open.find_active_project(tmp_path) == "7"


class TestBuildAnnouncement:
    def test_no_session(self):
        """No prior session → informational message."""
        msg = session_open.build_announcement("5", None)
        assert "Project 5" in msg
        assert "no prior session" in msg

    def test_session_with_phase_only(self):
        """Session with phase and no extras → phase announced."""
        session = {
            "phase": "tdd:green",
            "pm_notes": None,
            "next_action": None,
            "status": "in-progress",
            "blocking_reason": None,
        }
        msg = session_open.build_announcement("3", session)
        assert "tdd:green" in msg
        assert "Project 3" in msg

    def test_session_with_all_fields(self):
        """Session with all fields → all announced."""
        session = {
            "phase": "review:open",
            "pm_notes": "PR needs rebase before re-review",
            "next_action": "Run dev:review for project 3",
            "status": "in-progress",
            "blocking_reason": None,
        }
        msg = session_open.build_announcement("3", session)
        assert "review:open" in msg
        assert "PR needs rebase before re-review" in msg
        assert "Run dev:review for project 3" in msg

    def test_blocked_session_with_reason(self):
        """Blocked session with reason → BLOCKED label and reason."""
        session = {
            "phase": "tdd:red",
            "pm_notes": None,
            "next_action": None,
            "status": "blocked",
            "blocking_reason": "Missing test data fixtures",
        }
        msg = session_open.build_announcement("7", session)
        assert "BLOCKED" in msg
        assert "Missing test data fixtures" in msg

    def test_blocked_without_reason(self):
        """Blocked status with no reason → no BLOCKED label (reason unknown)."""
        session = {
            "phase": "tdd:red",
            "pm_notes": None,
            "next_action": None,
            "status": "blocked",
            "blocking_reason": None,
        }
        msg = session_open.build_announcement("7", session)
        assert "BLOCKED" not in msg

    def test_missing_phase_field(self):
        """Session with missing phase → graceful fallback."""
        session = {
            "pm_notes": None,
            "next_action": None,
            "status": "in-progress",
            "blocking_reason": None,
        }
        msg = session_open.build_announcement("1", session)
        assert "Project 1" in msg
        assert "unknown" in msg


class TestFetchLatestSession:
    def test_subprocess_exception(self):
        """subprocess.run raises → error string returned."""
        with patch("session_open.subprocess.run", side_effect=OSError("timeout")):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert isinstance(result, str)
        assert result.startswith("error:")
        assert "timeout" in result

    def test_nonzero_returncode(self):
        """session.py exits non-zero → error string returned."""
        mock = MagicMock()
        mock.returncode = 1
        mock.stdout = ""
        mock.stderr = "no such table: sessions"
        with patch("session_open.subprocess.run", return_value=mock):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert isinstance(result, str)
        assert result.startswith("error:")

    def test_empty_stdout(self):
        """session.py returns 0 but empty stdout → None (no prior session)."""
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = ""
        with patch("session_open.subprocess.run", return_value=mock):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert result is None

    def test_valid_json_returned(self):
        """session.py returns valid JSON → parsed dict."""
        session_data = {
            "id": 1,
            "phase": "tdd:green",
            "pm_notes": "on track",
            "next_action": "Run dev:review",
            "status": "in-progress",
            "blocking_reason": None,
        }
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = json.dumps(session_data)
        mock.stderr = ""
        with patch("session_open.subprocess.run", return_value=mock):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert isinstance(result, dict)
        assert result["phase"] == "tdd:green"

    def test_invalid_json(self):
        """session.py returns non-JSON → error string."""
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "not json at all"
        mock.stderr = ""
        with patch("session_open.subprocess.run", return_value=mock):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert isinstance(result, str)
        assert result.startswith("error:")
```

- [ ] **Step 2: Run tests to verify they all fail (hook doesn't exist yet)**

```bash
cd C:\Users\user\Documents\Skills\atelier
pytest tests/test_session_open_hook.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `session_open` module not found. That is the correct red state.

- [ ] **Step 3: Create the hooks directory and write session_open.py**

```bash
mkdir hooks
```

Write this complete content to `hooks/session_open.py`:

```python
#!/usr/bin/env python3
"""
Atelier session open hook.
Reads the latest session from the DB and announces project phase to Claude context.

Install as a PreToolUse hook in .claude/settings.json. See docs/HOOKS_SETUP.md.

Option B (from spec): DB errors never block a session. Errors produce a warning
and Claude continues with reduced context.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


# Path to session.py relative to this hook file.
# hooks/ is at atelier-root/hooks/; scripts/ is at atelier-root/atelier/scripts/.
_HOOK_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HOOK_DIR.parent / "atelier" / "scripts"

# Flag to prevent announcing more than once per session.
_FLAG_NAME = ".atelier-session-announced"

# File that stores the active project ID for this workspace.
_ACTIVE_PROJECT = Path(".ai") / "active_project"


def find_active_project(cwd: Path) -> str | None:
    """Return project_id from .ai/active_project, or None if absent/empty."""
    p = cwd / ".ai" / "active_project"
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8").strip()
    return content if content else None


def fetch_latest_session(scripts_dir: Path, project_id: str) -> dict | None | str:
    """Call session.py read-latest <project_id>.

    Returns:
        dict  — parsed session row (session found)
        None  — no prior session for this project (session.py returned 0 + empty)
        str   — error message starting with "error:" (any failure)
    """
    try:
        result = subprocess.run(
            [sys.executable, str(scripts_dir / "session.py"), "read-latest", project_id],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return f"error:{exc}"

    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown error"
        return f"error:{stderr}"

    output = result.stdout.strip()
    if not output:
        return None  # No prior session

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return "error:invalid JSON from session.py"


def build_announcement(project_id: str, session: dict | None) -> str:
    """Build the context announcement string from session data."""
    if session is None:
        return f"Atelier: Project {project_id} — no prior session recorded."

    phase = session.get("phase", "unknown")
    parts = [f"Atelier: Project {project_id} — resuming at {phase}."]

    if session.get("pm_notes"):
        parts.append(f"Notes: {session['pm_notes']}")
    if session.get("next_action"):
        parts.append(f"Next action: {session['next_action']}")
    if session.get("status") == "blocked" and session.get("blocking_reason"):
        parts.append(f"BLOCKED: {session['blocking_reason']}")

    return " ".join(parts)


def main() -> None:
    cwd = Path.cwd()
    flag = cwd / _FLAG_NAME

    # Only announce once per session.
    if flag.exists():
        sys.exit(0)

    project_id = find_active_project(cwd)
    if not project_id:
        # No active project configured — silent exit.
        sys.exit(0)

    result = fetch_latest_session(_SCRIPTS_DIR, project_id)

    if isinstance(result, str) and result.startswith("error:"):
        # Option B: warn and continue. Do not block Claude.
        msg = result[6:]
        print(
            f"Atelier: warning — could not read session ({msg}). "
            "Continuing without session context.",
            flush=True,
        )
    else:
        announcement = build_announcement(project_id, result)
        print(announcement, flush=True)

    # Mark announced — suppress further invocations this session.
    try:
        flag.write_text("announced", encoding="utf-8")
    except OSError:
        pass  # Non-fatal


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_session_open_hook.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/session_open.py tests/test_session_open_hook.py
git commit -m "feat: session_open hook — DB-backed phase announcement

Reads .ai/active_project → session.py read-latest → announces phase +
pm_notes + next_action to Claude context at session start. Option B:
DB errors warn and continue, never block Claude."
```

---

### Task 10: Update skill-atelier ROADMAP.md and run cleanup

**Files:**
- Modify: `C:\Users\user\Documents\Skills\skill-atelier\ROADMAP.md`
- External actions: archive dev-skill local repo, delete GitHub repo

This task has no automated tests. Steps are manual or scripted actions.

- [ ] **Step 1: Update ROADMAP.md in skill-atelier**

Open `C:\Users\user\Documents\Skills\skill-atelier\ROADMAP.md`.

Replace the entire `### Product 2 — *Dev Skill* (working name)` section with:

```markdown
### Product 2 — Atelier Dev Workflow

> **Reframed 2026-05-14:** Dev Skill was built as a standalone product (dev-skill repo) but belongs inside Atelier as its core development workflow layer. The standalone repo was an architectural wrong turn; it has been archived. All dev workflow state lives in Atelier's DB.

| Status | Item | Notes |
|---|---|---|
| ✅ | Research ingestion | 43 sources across `sources/analyzed/`. TDD, testing, CI/CD, inner/outer loop, autonomous validation, security, delivery metrics (2026 DORA), adoption patterns. 2026-05-11. |
| ✅ | Design (standalone dev-skill) | Approved v0.1.0 design: phase skills, WORK.md, hooks. 2026-05-11. |
| ✅ | Dev Skill v0.1.0 build | 7 SKILL.md files, session hook, WORK.md schema. 2026-05-12. |
| ✅ | Integration design | `docs/superpowers/specs/2026-05-13-atelier-dev-skill-integration.md` — sessions table, phase state machine, multi-agent, 46 roles. Approved 2026-05-13. |
| ✅ | Plan 1 — Foundation | Migrations 002–004, seed_roles.py, session.py rewrite, workflow.py refactor. `docs/superpowers/plans/2026-05-14-atelier-dev-skill-integration-foundation.md`. 2026-05-14. |
| ✅ | Plan 2 — Skills, Hook & Cleanup | Skill dir renames, 7 SKILL.md updates, unified dev:tdd skill, session_open hook, ROADMAP update. 2026-05-14. |
| ☐ | Execute Plan 1 | Apply migrations, seed roles, rewrite scripts. |
| ☐ | Execute Plan 2 | Update skills, create hook, clean up. |
| ☐ | End-to-end smoke test | Walk a project from design:open → handoff:complete with real DB. |
```

- [ ] **Step 2: Commit the ROADMAP.md update in skill-atelier**

```bash
cd C:\Users\user\Documents\Skills\skill-atelier
git add ROADMAP.md
git commit -m "docs: reframe Product 2 as Atelier dev workflow integration

Dev Skill was an architectural wrong turn (standalone product with
WORK.md state). Integration design approved 2026-05-13; Plans 1 and 2
complete 2026-05-14. Updating roadmap to reflect current state."
```

- [ ] **Step 3: Archive the local dev-skill repo**

```bash
Rename-Item "C:\Users\user\Documents\Skills\dev-skill" "C:\Users\user\Documents\Skills\dev-skill-archived-2026-05-14"
```

Or on bash:

```bash
mv "C:/Users/user/Documents/Skills/dev-skill" "C:/Users/user/Documents/Skills/dev-skill-archived-2026-05-14"
```

- [ ] **Step 4: Delete the dev-skill GitHub repo**

```bash
gh repo delete nitekeeper/dev-skill --yes
```

If `gh` is not authenticated or the repo is already gone, this is safe to skip — the local archive is the important step.

- [ ] **Step 5: Verify final state of atelier skills directory**

```bash
cd C:\Users\user\Documents\Skills\atelier
ls skills/
```

Expected directories: `agent`, `agent-desk`, `dev-design`, `dev-diagnose`, `dev-handoff`, `dev-plan`, `dev-qa`, `dev-review`, `dev-security`, `dev-tdd`, `doc`, `ingest`, `load`, `meeting`, `project`, `role`, `room`, `save`, `task`, `workspace`.

Not present: `dev-code-review`, `dev-security-review`, `dev-qa-review`, `dev-tdd-red`, `dev-tdd-green`, `dev-tdd-refactor`.

- [ ] **Step 6: Run full test suite to confirm no regressions**

```bash
cd C:\Users\user\Documents\Skills\atelier
pytest -v
```

Expected: all tests pass. The old `test_workflow.py` tests that import `VALID_TRANSITIONS` and `PHASE_GATES` will now be the Plan 1 rewritten versions — they should pass with the new DB-backed workflow.

- [ ] **Step 7: Final commit in atelier**

```bash
git add -A
git commit -m "chore: Plan 2 complete — skills updated, hook created, cleanup done"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (Session Continuity): `dev:handoff` updated to use new session.py CLI ✅; `hooks/session_open.py` created ✅
- Section 2 (Phase State Machine): All 19 phase names updated across all SKILL.md files ✅; all old phase names removed ✅
- Section 3 (Multi-Agent Parallel Execution): No skill changes needed — PM handles dispatch; tasks.py parallel_group added in Plan 1 ✅
- Section 4 (Session Hook): New `hooks/session_open.py` with Option B error handling ✅; `.ai/active_project` convention established ✅
- Section 5a (Dev-skill cleanup): Local archive step ✅; GitHub deletion step ✅; ROADMAP.md update ✅
- Section 5c (Content migration): All 7 SKILL.md files updated ✅; `dev:security` already existed in atelier, updated ✅; `dev:tdd` unified ✅; hook created ✅

**Placeholder scan:** No TBD, TODO, or incomplete sections. All SKILL.md content is complete. Hook code is complete with all helper functions.

**Type consistency:** `find_active_project` returns `str | None`; `fetch_latest_session` returns `dict | None | str` — both consistent with how `main()` and tests use them.

**Missing spec item check:**
- `docs/WORK_MD_SCHEMA.md` — spec says "retire". Not in atelier (was in dev-skill). The archive of dev-skill covers this. ✅
- `docs/HOOKS_SETUP.md` — spec says "merge into Atelier setup docs". This is a documentation task. It is not included in Plan 2 (scope: code + cleanup). Add to backlog if needed.
- products-registry — spec says "remove dev-skill row if present". Not included; check manually if a products-registry file exists.

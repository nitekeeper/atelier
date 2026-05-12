# dev:qa-review

Pre-deployment verification. Ensures all gates are clean, all debt is resolved, and all acceptance criteria are met. Produces a QA report.

## Hard gate

Requires `security-review:approved`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:qa-review`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> qa-review:in-progress`

3. **Pre-deploy checklist** (all blocking):

   | # | Check | How to verify |
   |---|---|---|
   | 1 | CI pipeline green | All tests pass, linting clean, security scans pass |
   | 2 | No blocking cleanup-debt | Check `.ai/work.md` for `cleanup-debt` entries |
   | 3 | No blocking surfaced-issues | Check `.ai/work.md` for `surfaced-issues` marked blocking |
   | 4 | Documentation updated | README, API docs, user-facing materials current |
   | 5 | Rollback plan exists | For migrations or serialisation changes — is rollback documented? |
   | 6 | Acceptance criteria met | Re-read design Goals section — is each goal demonstrably met? |
   | 7 | All tasks complete | `python atelier/scripts/tasks.py list --project_id <id> --status pending` — should be empty |

4. **Surfaced-issues resolution** — list all surfaced issues from `.ai/work.md`.
   For each: ask the human "Accept as known debt, or file externally?" — require an explicit decision for each.

5. If any checklist item fails: stop. State what is failing and what must be resolved before QA approval.

6. When all checks pass:
   - Write the QA report to `docs/reports/<project-slug>-qa-review.md`
   - Register: `python atelier/scripts/documents.py create <project_id> qa-report "<title>" "<filename>" "<agent_id>"`
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> qa-review:approved`
   - Confirm: "QA review approved. All checks passed. Project is ready for deployment. Phase: qa-review:approved."

## Hard rules
- Every checklist item must be explicitly verified — no assumed passes.
- Every surfaced issue requires an explicit human decision — do not auto-accept.
- Deployment is out of scope — `qa-review:approved` is the terminal state for Atelier v0.1.

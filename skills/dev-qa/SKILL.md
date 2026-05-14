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
   | 1 | CI pipeline green | Run `pytest -v` — all tests must pass. Run `python -m py_compile scripts/*.py` to check for syntax errors. |
   | 2 | All assigned tasks complete | `python atelier/scripts/tasks.py list --project_id <project_id> --status assigned` — must be empty |
   | 3 | No open blocking tasks | `python atelier/scripts/tasks.py list --project_id <project_id> --status open` — review any found |
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

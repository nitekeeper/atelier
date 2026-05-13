# dev:security-review

Reviews the implementation for security vulnerabilities. Produces a security report. Blocking issues must be resolved before advancing.

## Hard gate

Requires `code-review:merged`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:security-review`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> security-review:in-progress`

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
   - State all issues with file references and proposed fixes.
   - Do not advance the phase until all blocking issues are resolved and re-reviewed.

7. When clean:
   - Write the security review report to `docs/reports/<project-slug>-security-review.md`
   - Register: `python atelier/scripts/documents.py create <project_id> security-review-report "<title>" "<filename>" "<agent_id>"`
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> security-review:approved`
   - Confirm: "Security review complete. No blocking issues. Report saved. Ready for `dev:qa-review`."

## Hard rules
- No "no security concerns" without a documented reason why.
- Secrets in code are always blocking — no exceptions.
- Do not advance the phase if any blocking issue is unresolved.

# dev:security

Security review. Checks the implementation for vulnerabilities, exposed secrets, and insecure patterns before QA.

## Hard gate

Requires `review:approved`.

## Procedure

1. Run: `python atelier/scripts/workflow.py check-gate <project_id> dev:security`
   If the gate fails, state the current phase and stop.

2. Advance phase: `python atelier/scripts/workflow.py advance <project_id> security:open`

3. Security checklist (all required):

   | # | Check |
   |---|---|
   | 1 | No secrets, API keys, or credentials in source or test files |
   | 2 | No hardcoded internal paths, hostnames, or IPs |
   | 3 | All external input is validated before use |
   | 4 | SQL queries use parameterised statements — no string interpolation |
   | 5 | File paths derived from user input are sanitised (no path traversal) |
   | 6 | Dependencies are pinned — no floating version constraints. Check `requirements.txt` or `pyproject.toml`: no `>=`, `~=`, or unpinned entries |
   | 7 | Error messages do not leak internal state to external callers |
   | 8 | Authentication and authorisation are not bypassable by changing a parameter |

4. **If issues are found:**
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> security:changes-requested`
   - List each issue: file, line range, vulnerability class, recommended fix.
   - The engineer addresses all issues.
   - On re-review: advance back to `security:open` first:
     ```
     python atelier/scripts/workflow.py advance <project_id> security:open
     ```
     Then repeat the checklist from the top.

5. **If no issues found:**
   - Advance phase: `python atelier/scripts/workflow.py advance <project_id> security:approved`
   - Confirm: "Security review approved. Phase: security:approved. Ready for dev:qa."

## Hard rules
- Run `pytest -v` before approving — security fixes must not regress tests.
- Never mark an item as passed without explicitly checking it — no assumed passes.
- Secrets found anywhere in source are a hard block — no exceptions.

---
name: atelier-dev-receive-review
description: Use when review feedback has been posted — evaluates each item, pushes back on factual errors with evidence, implements accepted changes, and requests re-review.
---

# dev:receive-review

Implementer-side review processing. Reads all feedback before acting, classifies each item, verifies technical claims, implements accepted fixes, re-runs tests, and requests re-review. The reviewer's side is handled by `dev:review`.

## Hard gate

Requires `review:changes-requested`.

## Procedure

1. **Check the phase gate:**
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:receive-review
   ```
   Parse JSON output. Apply standard bypass-confirm-log flow if `allowed` is `false`.

2. **Read all feedback before acting on any item.** Do not touch code yet. Read every comment, suggestion, and objection in full.

3. **Classify each item** — assign one verdict to every piece of feedback before writing a single line of code:

   | Verdict | Condition |
   |---|---|
   | **Accept** | The feedback is factually correct and the change improves the code. |
   | **Clarify** | The feedback is ambiguous — you need more information before deciding. |
   | **Push back** | The feedback contains a verifiable factual error, or conflates a preference with a correctness issue. |

   Present the full classified list to yourself before proceeding. This list is the work order.

4. **Handle clarify items first.** Ask the reviewer the specific question needed to classify the item. Do not guess.

5. **Handle push-back items before implementing anything.**

   For each push-back:
   - State the disputed claim precisely.
   - Provide evidence (test output, spec reference, language documentation, benchmark).
   - Propose a resolution.
   - Log the pushback in the session notes via `python atelier/scripts/session.py update <session_id> --notes "<item: disputed claim / evidence / resolution>"`.

   **Distinction — factual vs. preference disputes:**
   - *Factual dispute:* reviewer claims the code is incorrect, unsafe, or misses a requirement. Push back with evidence if wrong.
   - *Preference dispute:* reviewer prefers a different naming convention, structural pattern, or stylistic choice. Do not push back unilaterally — surface the disagreement to the human: "The reviewer prefers X; I used Y because Z. Which do you want?"

   Do not silently capitulate to a factually incorrect claim because the reviewer repeats it. Do not treat a preference as a correctness issue. Both are failure modes.

6. **Implement accepted fixes** — work through the accept list. For each fix:
   - Make the change.
   - Run the targeted test: `pytest <test-file>::<test-name> -v` — confirm PASS.
   - Run the full suite: `pytest -v` — confirm 0 failures.
   - If any test fails: stop. Fix the regression before moving to the next accept item.

7. **Verify before requesting re-review** — after all accepted fixes are implemented:
   ```
   pytest -v
   ```
   All tests must pass. Do not request re-review with a failing suite.

8. **Advance phase and request re-review:**
   ```
   python atelier/scripts/workflow.py <db_path> advance <project_id> review:open
   ```
   Notify the reviewer: "Changes implemented for all accepted items. Push-back items logged. Ready for re-review."

## Hard rules

- Read all feedback before acting on any of it.
- Classify every item before touching code.
- Never silently agree to a technically wrong claim — push back with evidence or ask the human.
- Never request re-review with a failing test suite.
- Preference disputes go to the human; factual disputes get evidenced pushback.
- Push-back verdicts must be logged in the session notes — invisible pushbacks create repeat cycles.

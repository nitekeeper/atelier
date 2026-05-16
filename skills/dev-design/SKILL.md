---
description: Use when starting any new project, feature, or initiative — PM agent grills requirements until all design decisions are resolved.
user-invocable: false
---

# dev:design

The first phase of every project. The PM agent grills the human until all requirements are fully clear, then produces the design document. No time limit on questions — the PM keeps asking until there are zero remaining ambiguities.

## Hard gate

No gate — requires only that a project exists. Run `project:read <project_id>` to confirm.

## Procedure

1. Check the phase gate:
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:design
   ```
   Parse the JSON output `{"allowed": bool, "current_phase": str, "required_phase": str|null, "reason": str}`.
   For this skill `allowed` is always `true` (no gate configured). Record `current_phase` for later use, then proceed to the next step.
   - If the project does not exist, stop and tell the user to create one first with `project:create`.

2. **Grilling phase** — ask one question at a time until all of the following are unambiguous:
   - What problem does this project solve? Who experiences it?
   - What does success look like? How will it be measured?
   - What is explicitly out of scope?
   - What are the non-negotiable constraints (performance, security, compliance, deadlines)?
   - What systems or services does this touch?
   - Are there security or privacy concerns?
   - What alternatives were considered and why rejected?
   - What are the open questions or unknowns?
   Keep asking until you can answer all of the above without making any assumption.

3. **Draft the design document** with these required sections (in order):
   - **Goals** — what this project accomplishes; measurable outcomes
   - **Non-Goals** — what this project explicitly does NOT do
   - **Approach** — the chosen solution and why
   - **Alternatives Considered** — at least two alternatives and why each was rejected
   - **Cross-cutting concerns** — security, privacy, observability, rollback, compatibility
   - **Assumptions and dependencies** — external systems, versions, preconditions
   - **Open questions** — unresolved items with an owner for each

4. Present the draft to the human. Ask: "Does this capture everything? What should change?"
   Revise until the human explicitly approves.

5. Write the design document to a file (e.g. `docs/design/<project-slug>-design.md`).

6. Register the document: `python atelier/scripts/documents.py create <project_id> design "<title>" "<filename>" "<agent_id>"`

7. Advance phase: `python atelier/scripts/workflow.py <db_path> advance <project_id> design:approved`

8. Confirm: "Design document approved. Phase advanced to design:approved. Ready for `dev:plan`."

## Hard rules
- Never produce the design document before grilling is complete.
- Never advance the phase without explicit human approval.
- Alternatives Considered must contain at least two alternatives.
- The security/cross-cutting section is never empty — if no concerns, state why explicitly.

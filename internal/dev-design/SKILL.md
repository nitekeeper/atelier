---
description: Use when starting any new project, feature, or initiative — PM agent grills requirements until all design decisions are resolved.
---

# dev:design

The first phase of every project. The PM agent grills the human until all requirements are fully clear, then produces the design document. No time limit on questions — the PM keeps asking until there are zero remaining ambiguities.

> **Prerequisites**
> - Mode: Memex or Local (mode-symmetric — `backend.py` routes all phase and document writes)
> - Required: `project_id` known and project exists (`project:read <project_id>` confirms)
> - Required tables: `projects`, `skill_gates`, `project_documents` — seeded by Atelier bootstrap

## Hard gate

No gate — requires only that a project exists. Run `project:read <project_id>` to confirm.

## Procedure

1. Check the phase gate:
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:design
   ```
   Parse the JSON output `{"allowed": bool, "current_phase": str, "required_phase": str|null, "reason": str}`.
   For this skill `allowed` is always `true` (no gate configured). Record `current_phase` for later use, then proceed to the next step.
   - If the project does not exist, stop and tell the user to create one first with `internal/project/SKILL.md` (`create`).

2. **Grilling phase** — walk the human **section by section** through the 9-section spec template below, asking one question at a time and grilling each section against its specificity bar until every section is unambiguous. The PM phase is the most important phase for downstream correctness — every later worker reads the spec from their field's perspective, and a vague spec produces vague work. Keep asking until each section meets its bar without you making any assumption.

3. **Draft the design document** using the 9-section spec template (in order). This is the authoritative template from the team-mode design spec §6.2 (`docs/specs/2026-05-25-atelier-team-mode-design.md` — "The 9-section spec template"); each section must clear its specificity bar:

   | # | Section | Purpose | Specificity bar |
   |---|---|---|---|
   | 1 | **Goal** | Single-sentence outcome statement | Testable; no compound goals |
   | 2 | **Scope** | What is in | Each scope bullet is concretely demonstrable |
   | 3 | **Non-goals** | What is explicitly out | Each non-goal closes a likely interpretation ambiguity |
   | 4 | **Acceptance criteria** | How "done" is measured | Each criterion is testable (boolean or measurable) |
   | 5 | **Constraints** | What we may not do (perf, compat, etc.) | Named, not hand-waved |
   | 6 | **Stakeholders** | Who cares + their interest | Named role (not "the team") |
   | 7 | **Dependencies / Prerequisites** | What must exist before we can ship | Named systems / artifacts / decisions |
   | 8 | **Risks / Unknowns** | What we don't know + what could blow up | Each risk has a mitigation or an "accept" disposition |
   | 9 | **Success metrics** | How we know it worked after ship | Quantified or boolean |

4. Present the draft to the human. Ask: "Does this capture everything? What should change?"
   Revise until the human explicitly approves.

5. Write the design document to a file (e.g. `docs/design/<project-slug>-design.md`).

6. Register the document: `python3 atelier/scripts/documents.py create <project_id> design "<title>" "<filename>" "<agent_id>"`

7. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> design:approved`

8. Confirm: "Design document approved. Phase advanced to design:approved. Ready for `internal/dev-plan/SKILL.md`."

## Hard rules
- Never produce the design document before grilling is complete.
- Never advance the phase without explicit human approval.
- The document MUST contain all 9 sections from the §6.2 template, in order (Goal; Scope; Non-goals; Acceptance criteria; Constraints; Stakeholders; Dependencies / Prerequisites; Risks / Unknowns; Success metrics). No section may be omitted.
- Every section must clear its specificity bar. If a section genuinely has nothing to record (e.g. no external dependencies), state that explicitly rather than leaving it blank — a blank section is treated as "not yet grilled," not as "none."
- Acceptance criteria (§4) must be testable (boolean or measurable); Risks / Unknowns (§8) must give each risk a mitigation or an explicit "accept" disposition.

> **Note (creation vs. amendment).** This procedure CREATES the spec via `documents.py create` (step 6) — that path is unchanged. When an existing spec needs a versioned revision rather than a fresh doc, amend it through `scripts.documents.write_spec_amendment(db_path, prior_doc_id, title, body, created_by)` (atelier#62) — it writes a NEW `project_documents` row carrying `metadata={"version": n, "supersedes": <prior_id>}` and never mutates the prior row in place, preserving the spec's version chain. Creation here is unchanged; amendment is the separate spec-versioning path.

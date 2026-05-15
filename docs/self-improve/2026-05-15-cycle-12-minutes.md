# Self-Improvement Meeting — Cycle 12
**Date:** 2026-05-15 UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. Nadia Petrov | Agent Systems Architect |
| Dr. Fatima Al-Rashid | AI Safety Researcher |
| Dr. Yusuf Okafor | Prompt Engineer |
| Dr. Aisha Mensah | Cognitive Scientist |
| Dr. Yewande Diallo | AI Ethicist |
| Dr. Amara Osei-Bonsu | AI Research Scientist |

## PM Assessment
Two remaining superpowers gaps: `dev:subagent` (parallel subagent execution with two-stage review per task) and `dev:write-skill` (skill authoring methodology). Both are critical for atelier to be fully self-sufficient.

## Agenda
1. What procedure does `dev:subagent` need: dispatch model, two-stage review, task integration, stopping conditions?
2. What does `dev:write-skill` need: authoring structure, self-review gate, verification, governance guardrails?

## Discussion

### Agenda Item 1: `dev:subagent`

**Proposals:**
- Dr. Petrov + Dr. Okafor: 4-file design (SKILL.md + 3 prompt templates). Phase advances per task (tdd:red → tdd:clean). Re-dispatch limits: Stage 1 max 3 attempts, Stage 2 max 2. Tasks claimed under `subagent-implementer` identity. No review:open advance — defers to human.
- Dr. Mensah + Dr. Osei-Bonsu: Hard stop on non-recoverable error. Task ceiling of 10 before mandatory human checkpoint. Cascading state drift risk from per-task reviews not catching cross-task semantic drift.
- Dr. Diallo: Destructive tasks (file deletion, schema migration) need explicit human confirmation gates, not just logging. Continuous execution inappropriate for asymmetrically-reversible operations.

**Discussion:** Task ceiling of 10 adopted as a human checkpoint. Diallo's destructive gate adopted as `[DESTRUCTIVE]` tagging in plan tasks triggering confirmation prompt before dispatch. Hard stop on non-recoverable error adopted immediately (not after current batch).

**Decision:** 4-file structure with task ceiling, destructive gate, immediate hard stop — *Unanimous*

### Agenda Item 2: `dev:write-skill`

**Proposals:**
- Dr. Al-Rashid + Dr. Osei-Bonsu: Complete SKILL.md — 6-step procedure (name/location, required sections, step quality rules, self-review gate checklist, verification subagent, register). Hard rules: no frontmatter on dev skills, no logic in prose, bypass verbatim copy. Four failure modes in self-improve loop documented.
- Dr. Mensah: "One valid interpretation per step" gate condition — followability requires binary observable outcomes, not qualitative judgments.
- Dr. Diallo: Skills modifying Iron Laws or Hard rules require user review before verification subagent runs — closed loop where system validates its own constraints must be broken by human authority.

**Discussion:** Al-Rashid+Osei-Bonsu's SKILL.md adopted as base. Mensah's interpretation check added to self-review gate. Diallo's enforcement-skill review gate adopted in Hard rules.

**Decision:** Full SKILL.md with interpretation gate + enforcement-skill human review requirement — *Unanimous*

## Decisions Log
1. Create `skills/dev-subagent/SKILL.md` with task ceiling, destructive gate, hard stop — `skills/dev-subagent/`
2. Create `skills/dev-subagent/implementer-prompt.md`, `spec-reviewer-prompt.md`, `quality-reviewer-prompt.md`
3. Create `skills/dev-write-skill/SKILL.md` with 6-step procedure and governance guardrails — `skills/dev-write-skill/SKILL.md`

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | Create dev:subagent skill + prompt templates | `skills/dev-subagent/` | Dr. Nadia Petrov / Dr. Yusuf Okafor |
| 2 | Create dev:write-skill | `skills/dev-write-skill/SKILL.md` | Dr. Fatima Al-Rashid / Dr. Amara Osei-Bonsu |

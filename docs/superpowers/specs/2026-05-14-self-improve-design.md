# Atelier Self-Improvement Skill — Design Spec

**Date:** 2026-05-14
**Status:** Approved
**Author:** Dr. Priya Nair (PM), nitekeeper

---

## Goal

Enable Atelier to improve itself autonomously through a structured, multi-agent, meeting-driven cycle. The skill analyzes the full codebase, convenes a domain-relevant team of expert agents, reaches unanimous consensus on changes, implements them in an isolated clone, validates them, and merges to main — all with a complete paper trail. Users control when and how many cycles run.

---

## Invocation

```
dev:self-improve [--cycles N] [--subject "<area to improve>"]
```

- `--cycles N` — number of independent improvement cycles to run (default: 1)
- `--subject` — optional focus area (e.g., `"improve security review procedures"`, `"harden QA skill"`)

**Hard constraint:** This skill may only be initiated by a human user. No agent may call `dev:self-improve` directly. The skill procedure begins with an explicit check confirming human initiation; if called from within an agent workflow, it aborts immediately.

Cycles run sequentially. Each cycle is fully independent — no state, decisions, or context carry between cycles. A failure in one cycle is logged and does not block subsequent cycles.

---

## Architecture Overview

Each cycle follows five phases:

```
1. Agenda setting (PM)
      ↓
2. Domain agent selection + parallel pre-analysis
      ↓
3. Synthesis meeting → unanimous consensus
      ↓
4. Implementation in experiment clone
      ↓
5. Quality gates → push → cleanup → pull
```

---

## Phase 1 — Agenda Setting

Dr. Priya Nair (PM subagent) reads the full repo:

- All `skills/*/SKILL.md` files
- All `scripts/*.py` files
- All `migrations/*.sql` files
- All `tests/` files
- `docs/`, `CHANGELOG.md`, `CLAUDE.md`

**If `--subject` is provided:**
PM reads the codebase with that subject as the lens. The agenda is scoped to that area. Agents summoned are those whose domain intersects the subject.

**If no subject:**
PM performs a full codebase audit and decides which area most needs improvement. Her reasoning is recorded in the minutes under a **"PM Assessment"** section before the agenda items.

The PM produces:
- A structured agenda (numbered items, each with a clear improvement question)
- A list of agents to summon (drawn from the 61-role roster, selected by domain relevance)
- The opening section of the meeting minutes document

---

## Phase 2 — Domain Agent Selection and Parallel Pre-Analysis

**Agent selection:** PM selects from the 61-role roster based on agenda content. The roster now includes 15 world-class AI experts whose domain maps directly to self-improvement work. Examples:

| Agenda area | Agents summoned |
|---|---|
| Skill procedures | Prompt Engineer, Agent Systems Architect, NLP Engineer, Cognitive Scientist |
| Agent reasoning / logic | AI Research Scientist, RL Researcher, Agent Systems Architect |
| Scripts / DB | Systems Engineer, Backend Engineers, AI Infrastructure Engineer, Architect |
| Safety and alignment | AI Safety Researcher, AI Ethicist, AI Policy Researcher |
| Test coverage | QA Engineers, Data Scientist (AI Evaluation) |
| Knowledge representation | Knowledge Engineer, NLP Engineer |
| Documentation | Technical Writer, AI Product Manager, PM |
| Cross-cutting / broad | Multiple AI experts + domain leads — PM decides scope |

**AI expert roles with standing relevance to self-improvement cycles:**

| Role | Why always relevant |
|---|---|
| Agent Systems Architect (Dr. Nadia Petrov) | Reviews agent orchestration, coordination logic, skill dispatch |
| AI Safety Researcher (Dr. Fatima Al-Rashid) | Checks every proposed change for alignment and failure modes |
| Prompt Engineer (Dr. Yusuf Okafor) | Reviews all SKILL.md procedures and prompt quality |
| AI Ethicist (Dr. Yewande Diallo) | Flags bias, harmful impacts, and governance concerns |
| AI Research Scientist (Dr. Amara Osei-Bonsu) | Validates theoretical soundness of reasoning patterns |
| Cognitive Scientist (Dr. Aisha Mensah) | Ensures procedures align with human cognitive principles |

All selected agents are named in the meeting minutes with their role and the agenda items they own.

**Parallel pre-analysis:** All summoned agents are dispatched simultaneously. Each independently reads the codebase areas relevant to their domain and produces a written proposal covering:

1. What they found (specific files, functions, patterns)
2. What they propose to change and why
3. Risk classification: destructive or non-destructive (see definitions below)
4. Any dependencies or conflicts with other agents' domains they anticipate

Proposals are collected before the meeting begins.

---

## Phase 3 — Synthesis Meeting and Unanimous Consensus

PM facilitates a structured meeting. The meeting follows the format below and is written in full to `docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md` as a **Markdown document**.

### Meeting Minutes Format

```markdown
# Self-Improvement Meeting — Cycle N
**Date:** YYYY-MM-DD HH:MM UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. [Name] | [Role] |
...

## PM Assessment *(only if no --subject provided)*
[PM's reasoning for choosing this area]

## Agenda
1. [Improvement question / area]
2. ...

## Discussion

### Agenda Item 1: [Title]
**Proposals received:**
- [Agent]: [Proposal summary]
- [Agent]: [Proposal summary]

**Discussion:**
[Debate summary — objections raised, how they were resolved]

**Decision:** [Agreed change, specific and actionable] — *Unanimous*
*OR*
**Decision:** DROPPED — [reason no consensus reached]

### Agenda Item 2: [Title]
...

## Decisions Log
1. [Decision text] — [file(s) affected]
2. ...

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | [what] | [where] | [agent] |
...

## Outcome
[PASSED / FAILED / FLAGGED FOR HUMAN APPROVAL]
[Reason if not PASSED]
```

**Consensus rule:** Every agenda item requires unanimous agreement from all summoned agents to proceed. If any agent objects, the item is revised until all agree, or dropped. No change enters implementation with a dissenting vote. Dropped items are recorded as "DROPPED" in the minutes.

---

## Phase 4 — Implementation in the Experiment Clone

### Clone setup

A sibling directory is created outside the production repo:

```
C:/Users/user/Documents/Skills/
  atelier/                          ← production repo (never touched during cycle)
  experiment/
    atelier/                        ← fresh clone of main branch
```

All changes are made exclusively inside `experiment/atelier/`. The production repo is read-only during implementation.

### Branch

A feature branch is created inside the clone before any changes are made:

```
self-improve/cycle-N-YYYY-MM-DD
```

Implementing agents make their assigned changes in this branch. The meeting minutes document is written to `docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md` inside the clone and included in the commit.

---

## Phase 5 — Quality Gates, Push, and Cleanup

### Gate 1 — Unanimous consensus (enforced in Phase 3)

No change enters the clone until all agents have agreed. Enforced before Phase 4 begins.

### Gate 2 — Destructive change detection

After changes are written, the diff is scanned for destructive patterns. A change is **destructive** if it:

- Deletes a file that other files import or reference
- Removes or renames a public function or CLI command
- Adds a DB migration that drops or renames columns or tables
- Removes or renames a skill directory
- Removes tests

If destructive changes are detected, implementation **pauses**. The user is notified:

> "Cycle N proposes a destructive change: [plain-language description of what changes and why it is destructive]. Approve this change? (y/n)"

- **Approved:** proceed to Gate 3
- **Rejected:** that change is dropped; non-destructive changes continue to Gate 3

### Gate 3 — Full test suite

```bash
python -m pytest -v
```

Run inside `experiment/atelier/`. All tests must pass.

- **Pass:** proceed to commit and push
- **Fail:** cycle is aborted. Failure is logged in the minutes under `Outcome: FAILED`. The branch is NOT pushed. `experiment/` is deleted. The next cycle (if any) starts fresh.

### Commit format

One commit per cycle:

```
self-improve(cycle-N): <one-line summary of changes>

Meeting: docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md
Participants: [comma-separated agent list]
Decisions:
  1. [decision text]
  2. [decision text]
  ...
Tests: N passed
Subject: [user-provided subject or "PM-directed"]
```

The minutes file is committed in the same commit as the code changes.

### Push and merge

```bash
git push origin self-improve/cycle-N-YYYY-MM-DD
```

**Auto-merge conditions** (both must be true):
- All tests passed
- No destructive changes (or all destructive changes were user-approved)

If auto-merge conditions are met:
```bash
git checkout main && git merge --no-ff self-improve/cycle-N-YYYY-MM-DD
git push origin main
git branch -d self-improve/cycle-N-YYYY-MM-DD
```

If destructive changes were flagged but not yet resolved — branch is pushed, merge waits for user confirmation.

### Cleanup

Regardless of outcome, `experiment/` is deleted after push (or abort):
```bash
rm -rf C:/Users/user/Documents/Skills/experiment/
```

If auto-merged: production repo pulls main:
```bash
cd C:/Users/user/Documents/Skills/atelier
git pull
```

If awaiting human approval: pull happens after the user confirms the merge.

---

## Cycle Summary Report

After each cycle completes (pass, fail, or pending approval), the skill prints a summary:

```
Cycle N — [PASSED / FAILED / AWAITING APPROVAL]
Subject: [subject or PM-directed]
Participants: [N agents]
Decisions: [N agreed / M dropped]
Changes: [files modified]
Minutes: docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md
Branch: self-improve/cycle-N-YYYY-MM-DD [merged / pending / not pushed]
```

---

## Hard Rules

1. **User-only invocation.** No agent may call this skill. Abort immediately if called from an agent context.
2. **Unanimous consensus required.** No change is implemented without agreement from all summoned agents.
3. **Tests must pass.** A failing test suite aborts the cycle — no exceptions.
4. **Destructive changes require human approval.** The skill never auto-merges a destructive change without explicit user confirmation.
5. **Experiment clone is isolated.** The production repo is never modified during a cycle. All changes happen in `experiment/atelier/`.
6. **Minutes are Markdown.** Every cycle produces a complete, well-formed Markdown meeting minutes document committed alongside the changes.
7. **One commit per cycle.** All changes from a cycle land in a single commit with the full decision log in the message.
8. **Cleanup always runs.** `experiment/` is deleted whether the cycle passes, fails, or is aborted — no leftover state.

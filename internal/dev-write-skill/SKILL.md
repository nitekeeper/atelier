---
description: Use when creating a new Atelier skill or rewriting an existing one — structures authoring, runs a self-review gate, and verifies with a subagent before registering.
---

# dev:write-skill

Authors, reviews, and registers a new Atelier skill. Use when creating a skill from scratch, rewriting an existing one, or auditing a skill for quality after a self-improve cycle identifies a gap.

## Hard gate

None — callable from any phase.

## Procedure

### 1. Name and location

- Directory: `skills/<name>/` — kebab-case, no `dev:` prefix in the folder name (the prefix is the invocation alias only).
- File: `skills/<name>/SKILL.md`
- Frontmatter: add YAML frontmatter (`name:`, `description:`) **only** for session-lifecycle skills (`ingest`, `save`, `load`) or the methodology loader (`execute`). Dev-workflow and CRUD skills omit frontmatter — they are routed through `execute`'s trigger contract, not by description scan. Adding frontmatter to a dev skill creates phantom triggers.

### 2. Required sections (in order)

1. H1 title — the invocation alias (e.g. `# dev:write-skill`)
2. One-line purpose — what the skill does; do not repeat the trigger condition
3. `## Hard gate` — the required phase, or "None — callable from any phase"
4. `## Procedure` — numbered steps; each is one atomic action or decision
5. `## Hard rules` — invariants phrased as prohibitions or requirements; never empty

### 3. Step quality rules

- Each step has exactly one imperative verb. "Read X and decide Y" is two steps.
- Steps that call a script include the exact command with placeholder tokens (e.g. `<project_id>`).
- Decision branches are inline (if/else), not deferred to a later step.
- The bypass procedure for phase-gated skills must be copied verbatim from `execute/SKILL.md` — do not paraphrase it.
- Every output artifact is named: file path, DB record, or phase transition.

### 4. Self-review gate

Run this checklist before writing the file. Any `no` blocks writing.

| # | Check |
|---|---|
| 1 | Every step has exactly one imperative verb |
| 2 | Every step has exactly one valid interpretation — no qualitative judgments ("handle appropriately", "as needed") |
| 3 | No step says anything that depends on the reader's taste or context |
| 4 | All script invocations include exact CLI form with placeholder tokens |
| 5 | Hard rules section is present and non-empty |
| 6 | Phase gate step (if present) uses canonical bypass pattern verbatim |
| 7 | Frontmatter is present only if skill is lifecycle or methodology-loader |
| 8 | No step embeds logic that belongs in a Python script |
| 9 | Every output artifact is named |

### 5. Enforcement-skill review gate

If the skill adds or modifies an `## The Iron Law` section or changes any `## Hard rules` content: **stop before running the verification subagent**. Show the draft to the user:

> "This skill modifies enforcement behavior. Please review before I verify it with a subagent."

Wait for explicit user approval. This breaks the closed loop where a self-improving system validates its own constraints.

### 6. Verification — dispatch a subagent

After the self-review gate (and enforcement gate if applicable), dispatch a fresh agent with:

> "Read `skills/<name>/SKILL.md` in full and execute the procedure against a test project. Report every step you took, every command you ran, and every place the instructions were unclear, ambiguous, or left you with more than one valid interpretation."

Accept the skill only if the agent trace shows: no invented steps, no omitted steps, no hesitation points, no ambiguous branch taken by assumption. A single ambiguity requires a prose fix before the skill ships.

### 7. Register

Skill discovery is file-based — no registration script exists. After writing:

1. Confirm the file exists at `skills/<name>/SKILL.md`.
2. Confirm no name collision: `ls skills/` — ensure the directory is new or intentionally replacing the old skill.
3. If replacing an existing skill, confirm the old file is overwritten, not duplicated.
4. If the skill has frontmatter, verify it parses as valid YAML.

## Hard rules

- Never add frontmatter to dev-workflow or CRUD skills.
- Never embed deterministic logic in a skill step — if it can be a Python function, it belongs in `scripts/`.
- Never paraphrase the bypass procedure — copy it verbatim from `execute/SKILL.md`.
- A skill with any ambiguous step must not be committed — fix first.
- Self-review gate runs before writing the file, not after.
- Skills that add or modify enforcement behavior (Iron Law, Hard rules) require explicit user review before verification.

# CLAUDE.md — Atelier

Atelier is a shared workspace for a human developer and a multi-agent system working together on the same project. It requires Memex to be set up in the target project.

## Hard dependency

**Memex must be set up before any Atelier command will work.** If `.ai/wiki/` and `memex.db` are absent, refuse and instruct the user to set up Memex first.

## Setup (once per project)

1. Install runtime dependencies: `pip install -r requirements.txt`
2. Run the DB migration from the **target project root**:
   ```bash
   PYTHONPATH=/path/to/atelier python /path/to/atelier/scripts/migrate.py .ai/memex.db
   ```
   - `PYTHONPATH` must point to the Atelier root — the scripts use `from scripts.db import ...` internally
   - Default db path: `.ai/memex.db`
3. Ensure `memex.db` is in WAL mode (the migration script handles this via `db.py`)
4. Add Atelier working directories to `.git/info/exclude` (not `.gitignore`) so they stay out of the project repo:
   ```
   .ai/
   lessons/
   ```
   Unlike `.gitignore`, this file lives only on your machine and is never committed — the project repo stays completely unaware of Atelier. Verify with `git status`: no output means the directories are invisible to git.

## Scripts

All deterministic operations live in `scripts/`. Each script is callable from the CLI:

| Script | Purpose |
|---|---|
| `scripts/db.py` | DB connection with WAL + FK enforcement |
| `scripts/migrate.py` | Idempotent migration runner |
| `scripts/session.py` | Read/write `.ai/work.md` session state |
| `scripts/roles.py` | Role CRUD |
| `scripts/agents.py` | Agent CRUD |
| `scripts/projects.py` | Project CRUD + phase tracking |
| `scripts/documents.py` | Project document CRUD |
| `scripts/tasks.py` | Task CRUD + assign/claim/complete flow |
| `scripts/meetings.py` | Meeting CRUD + `.ai/meetings/*.md` file write |
| `scripts/workflow.py` | Phase gate enforcement + transition validation |
| `scripts/workspace.py` | tmux session/window/pane management via libtmux |

## Skills

Skills live in `skills/<name>/SKILL.md`. Each is a thin wrapper that invokes a Python script and handles language tasks (grilling, summarizing, generating documents). Invoke via the `Skill` tool.

## Working rules

1. **Never skip the Memex check.** Every command must verify Memex is present before acting.
2. **Python scripts do the work.** Skill files handle only irreducible language tasks. Do not re-implement logic that belongs in a script.
3. **Phase gates are strict.** `workflow.py` enforces valid transitions. Do not bypass them.
4. **WAL mode is required.** All DB connections go through `scripts/db.py`. Never connect directly.
5. **Meetings write two places.** `meetings.py` writes both a DB record and `.ai/meetings/YYYY-MM-DD-<slug>.md`. Both must stay in sync.

## DB path convention

Default: `.ai/memex.db` (inside the target project). Scripts accept `db_path` as a positional argument.

## Auto-trigger architecture

Atelier's methodology lives in a single canonical file (`skills/using-atelier/SKILL.md`) and is surfaced through four mechanisms:

1. **SessionStart hook** (`hooks/session_start.py`) — injects the canonical body as system context every session
2. **`session_open.py` extension** — appends phase-specific guidance after the existing phase announcement
3. **CLAUDE.md template snippet** (`templates/CLAUDE-snippet.md`) — short backup methodology for consumer projects
4. **YAML frontmatter** on four session-lifecycle skills (`using-atelier`, `ingest`, `save`, `load`)

When the methodology changes, edit only `skills/using-atelier/SKILL.md`. The hooks parse this file on every invocation, so changes propagate without redeployment. The CLAUDE.md snippet is the only mechanism that requires manual sync; keep it minimal.

## Soft walls

Phase gates (in `skill_gates` table) are advisory, not enforced. `workflow.py check_gate` returns a `GateResult` describing whether the current phase satisfies the gate; it never raises on phase mismatch (it CAN raise `WorkflowError` for unknown `project_id` — a programming error, not a soft-wall concern). Skills are responsible for the bypass-confirm-log flow when `allowed=False`. Bypasses are recorded in `phase_bypasses` and surfaced by `dev:handoff` retros.

Hard rule: **never reintroduce raising in `check_gate` for phase mismatch.** If a downstream change makes the soft-wall flow feel insufficient, fix it at the policy layer (the `using-atelier` bypass procedure), not by re-walling the gate.

## Skill frontmatter convention

Most Atelier skills (`skills/<name>/SKILL.md`) open directly with `# <name>` and have no YAML frontmatter — they are invoked by name, not discovered by description.

Four skills carry YAML frontmatter for downstream tool discovery:
- `using-atelier` — loaded by the SessionStart hook (`hooks/session_start.py`) and discovered by agent skill-routing systems.
- `ingest`, `save`, `load` — session-lifecycle skills whose `description: Use when…` triggers help the agent route session events.

The frontmatter format is:

```yaml
---
name: <skill-name>
description: Use when <trigger condition> — <effect summary>.
---
```

Dev-workflow and CRUD skills do NOT use frontmatter — they are routed through `using-atelier`'s trigger contract.

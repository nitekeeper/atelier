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

<!-- TODO(Task 14): Document the skill frontmatter convention now that using-atelier (Task 4), 
     SessionStart hook (Task 5), and frontmatter on ingest/save/load (Task 8) have all shipped. -->

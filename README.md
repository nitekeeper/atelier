# Atelier

A shared workspace for a human developer and a multi-agent system working together on the same project.

## Requirements

- Python 3.11+
- [Memex](https://github.com/nitekeeper/memex) set up in the target project
- tmux (for workspace commands)

## Setup

```bash
# 1. Install runtime dependencies
pip install -r requirements.txt

# 2. Run the DB migration (from your project root)
python /path/to/atelier/scripts/migrate.py .ai/memex.db
```

That's it. The migration is idempotent — safe to re-run.

## Dev setup

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Skills

| Category | Skills |
|---|---|
| Session | `ingest`, `save`, `load` |
| Dev workflow | `dev:design`, `dev:plan`, `dev:tdd-red`, `dev:tdd-green`, `dev:tdd-refactor`, `dev:code-review`, `dev:security-review`, `dev:qa-review`, `dev:diagnose`, `dev:handoff` |
| Projects | `project:create/read/update/delete/list/search` |
| Documents | `doc:create/read/update/delete/list/search` |
| Tasks | `task:create/assign/claim/update/complete/list/search` |
| Agents | `agent:create/read/update/delete/list/search` |
| Roles | `role:create/read/update/delete/list/search` |
| Meetings | `meeting:create/read/update/delete/list/search` |
| Workspace | `workspace:create/join/list/leave`, `room:create/join/list/close`, `agent:join/leave` |

## Architecture

- All deterministic operations are Python scripts in `scripts/`
- Skill files in `skills/` are thin wrappers — they invoke scripts and handle language tasks only
- State lives in `memex.db` (8 tables) and `.ai/work.md` (session state)
- WAL mode required on `memex.db` for safe concurrent agent writes
- Workspace commands require tmux via `libtmux`

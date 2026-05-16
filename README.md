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
```

```bash
# 2. Run the DB migration (from your project root) — macOS/Linux
PYTHONPATH=/path/to/atelier python /path/to/atelier/scripts/migrate.py .ai/memex.db
```

```powershell
# Windows (PowerShell)
$env:PYTHONPATH = 'C:\path\to\atelier'; python C:\path\to\atelier\scripts\migrate.py .ai\memex.db
```

```cmd
:: Windows (CMD)
set PYTHONPATH=C:\path\to\atelier && python C:\path\to\atelier\scripts\migrate.py .ai\memex.db
```

That's it. The migration is idempotent — safe to re-run.

> **Note:** `PYTHONPATH` must point to the Atelier root (where `scripts/` lives) whenever
> running Atelier scripts directly. The skills handle this automatically; it only matters
> when invoking scripts by hand.

### Auto-trigger setup

Atelier ships a SessionStart hook that injects its methodology into every Claude Code session, so the agent knows the trigger contract and the soft-wall bypass procedure from the first user message.

Add to your project's `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/atelier/hooks/session_start.py"
          }
        ]
      }
    ]
  }
}
```

As a fallback if the hook can't run, paste `templates/CLAUDE-snippet.md` into your project's `CLAUDE.md`.

### Soft walls

Phase gates are recommendations, not blocks. When a dev skill detects an out-of-phase invocation, it asks you to confirm a bypass, then logs the bypass to `phase_bypasses` for retrospective. Run `dev:handoff` at project close to see the bypass summary.

### Keep Atelier out of your project's repo

Use `.git/info/exclude` rather than `.gitignore` to hide the Atelier working directories
from git. Unlike `.gitignore`, this file lives only on your machine and is never committed
— so your ignore rules stay completely invisible to the project repo.

```
# .git/info/exclude
.ai/
lessons/
```

Verify with `git status` — no output means git sees nothing untracked or modified. The
`.ai/` and `lessons/` directories are fully invisible to the repo.

## Dev setup

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Skills

| Category | Skills |
|---|---|
| Session | `run`, `ingest`, `save`, `load` |
| Dev workflow | `dev:design`, `dev:plan`, `dev:tdd`, `dev:review`, `dev:security`, `dev:qa`, `dev:diagnose`, `dev:handoff` |
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
- State lives in `memex.db` (13 tables including `phase_bypasses` for soft-wall audit, `sessions` for session state, etc.)
- WAL mode required on `memex.db` for safe concurrent agent writes
- Workspace commands require tmux via `libtmux`

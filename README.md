# Atelier

A shared workspace and methodology for a human developer collaborating with one or more AI agents on a software project. Atelier runs in two modes — **Local** (project-only SQLite, zero external dependencies) or **Memex** (writes flow into your Memex knowledge store so projects, decisions, and meetings become first-class indexed content). A project starts Local and can migrate to Memex later without losing history.

## Requirements

- Python 3.11+
- tmux (only for `workspace:*` and `room:*` commands)
- [Memex](https://github.com/nitekeeper/memex) is **optional** — install it if you want shared knowledge-store backing; otherwise Atelier runs fine on its own.

## Setup

```bash
git clone https://github.com/nitekeeper/atelier.git
cd atelier
pip install -r requirements.txt
```

That's it. There is **no manual migration step**. The first time you invoke an Atelier command from a project, Atelier detects the environment and provisions storage on its own:

- **Memex not installed** → Local mode. Atelier creates `.ai/atelier.db` in the current project and applies both `migrations/shared/` and `migrations/local-only/` automatically.
- **Memex installed and bootstrapped** → Memex mode. Atelier piggybacks on `memex:core:create-store` to provision `~/.memex/atelier.db` with the shared schema; local-only tables stay out.

Both bootstraps are idempotent — safe to invoke again, no-op on the second run.

### Where state lives

| Mode | Database | Notes |
|---|---|---|
| Local | `<project>/.ai/atelier.db` | Project-scoped, never leaves the project tree |
| Memex | `~/.memex/atelier.db` | Shared across all projects on this machine; indexed by Memex |

WAL mode is enforced on both paths. Concurrent agent writes are safe.

### Pointing Claude Code at the plugin

Atelier ships as a Claude Code plugin. From inside a Claude Code session, register it once:

```
agora:plugin-register --url https://github.com/nitekeeper/atelier.git
```

`/atelier:run`, `/atelier:load`, `/atelier:save`, `/atelier:ingest`, and `/atelier:migrate` become available immediately.

### Auto-trigger setup (recommended)

Atelier ships a `SessionStart` hook that injects its methodology into every Claude Code session — the agent knows the trigger contract and bypass procedure from the first user message, without needing a `CLAUDE.md` reminder.

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
            "command": "python3 /path/to/atelier/hooks/session_start.py"
          }
        ]
      }
    ]
  }
}
```

If the hook can't run in your environment, paste `templates/CLAUDE-snippet.md` into the project's `CLAUDE.md` as a fallback.

### Keep Atelier out of your project's repo

Use `.git/info/exclude` rather than `.gitignore` so the ignore rules live only on your machine and never get committed:

```
# .git/info/exclude
.ai/
lessons/
```

Verify with `git status` — no output means the project repo sees nothing untracked or modified. `.ai/` and `lessons/` are fully invisible to the upstream repo.

## Migration: Local → Memex

When Memex is installed *after* you've already started using Atelier locally, the next `/atelier:run`, `/atelier:load`, `/atelier:save`, or `/atelier:ingest` invocation in that project detects the mismatch and prompts:

> Memex is installed and bootstrapped, but this project still has a project-local atelier database. Migrate to Memex now? (y/N)

- **`y`** — projects, documents, decisions, meetings, and tasks are replayed into `~/.memex/atelier.db` via `internal/migrate-local-to-memex/SKILL.md`. The procedure is idempotent (already-replayed rows are detected via `source_ref` and skipped), the local DB is preserved until the migration succeeds, and `.ai/atelier.migrated` is written on success.
- **`N`** — Atelier writes `.ai/atelier.local-only` and continues in Local mode. The auto-prompt won't fire again for this project.

### Manual trigger: `/atelier:migrate`

Use the explicit slash command when:

- You answered `N` previously and want to opt back in. `/atelier:migrate` clears `.ai/atelier.local-only`.
- A prior auto-migration failed partway through. Fix the root cause (disk space, Memex bootstrap, etc.) and re-run.
- You're scripting migration across many projects — invoke `/atelier:migrate` from each project root rather than waiting for the auto-prompt at next session-open.

The manual path calls the same internal procedure as the auto-prompt, so guarantees (idempotency, crash-safety, row-count summary) are identical.

## Methodology

Atelier defines a default dev arc and trigger contract that every session follows. The full contract lives in `skills/run/SKILL.md`; the short version:

1. **Pre-flight.** Detect mode (Local / Memex / migrate-needed). On a fresh session, identify the active project and current phase.
2. **Trigger contract.** When the user describes new development work, ask one of three routings:
   - **(a) Full Atelier arc** — design → plan → tdd → review → security → qa → handoff, with soft phase walls at each step.
   - **(b) Bug fix** — `dev:diagnose` against the active project; writes a regression test first, restores phase on resolution.
   - **(c) Handle directly** — do the work without Atelier orchestration.
3. **Soft walls.** Phase gates are recommendations, not blocks. When a dev skill detects an out-of-phase invocation, it asks the user to confirm a bypass and logs it to `phase_bypasses` for retrospective. `dev:handoff` surfaces the bypass summary at project close.
4. **User authority.** Explicit user instructions override the methodology at all times. "Skip Atelier" is always a valid answer.

The methodology applies identically in both modes; only the storage backend changes.

## Skills

| Category | Skills |
|---|---|
| Session | `run`, `ingest`, `save`, `load`, `migrate` |
| Dev workflow | `dev:design`, `dev:plan`, `dev:tdd`, `dev:review`, `dev:security`, `dev:qa`, `dev:diagnose`, `dev:handoff` |
| Projects | `project:create/read/update/delete/list/search` |
| Documents | `doc:create/read/update/delete/list/search` |
| Tasks | `task:create/assign/claim/update/complete/list/search` |
| Agents | `agent:create/read/update/delete/list/search` |
| Roles | `role:create/read/update/delete/list/search` |
| Meetings | `meeting:create/read/update/delete/list/search` |
| Workspace | `workspace:create/join/list/leave`, `room:create/join/list/close`, `agent:join/leave` |

Session and migration commands are public slash commands (`/atelier:<name>`); dev-workflow and CRUD procedures are internal — agents read them from `internal/<name>/SKILL.md` via the Read tool rather than invoking them as slash commands.

## Dev setup

```bash
pip install -r requirements-dev.txt
pytest tests/
```

The test suite covers both backends in parallel, so a single `pytest` run validates Local and Memex behavior together.

## Architecture (one-line summary)

Deterministic operations live in Python scripts under `scripts/`. Skill files are thin language wrappers around those scripts. The backend split (`scripts/backend_local.py` for project-local SQLite, `scripts/backend_memex.py` for the Memex store) is hidden behind a single mode-dispatching API in `scripts/backend.py`; callers never branch on mode directly. See `CLAUDE.md` for internal architecture details.

# CLAUDE.md — Atelier

Atelier is a shared workspace methodology for a human developer and a multi-agent system working together on the same project. It runs in either of two modes — automatically detected:

| Mode | When | Backend |
|---|---|---|
| **Memex** (preferred) | Memex v2 is installed in Claude Code | `~/.memex/atelier.db` registered as a Memex Core store; documents indexed in `~/.memex/index.db`; raw bodies archived to `~/.memex/raw/` |
| **Local** (fallback) | Memex is absent | `<project-root>/.ai/atelier.db` with FTS5-only retrieval. No federated index, no vector search. |

You never configure the mode — every Atelier command runs `scripts.atelier_entrypoint.startup_check()` first and routes to the right backend.

## Dependency posture

Atelier no longer requires Memex to be installed. If Memex is present, Atelier uses it. If not, Atelier runs locally with full feature parity except cross-project search and embeddings.

## Setup

### Memex mode (zero ceremony)

On the first Atelier command when Memex is detected, bootstrap runs automatically:

- Seeds Atelier's roles + agents into `~/.memex/agents.db`.
- Creates the `atelier` store via `memex:core:create-store` (applies only `migrations/shared/`).
- Writes `~/.memex/atelier.bootstrap.json` as the idempotency marker.

The bootstrap is idempotent. You never invoke it directly.

### Local mode (per project)

The first Atelier command in a repo creates `.ai/atelier.db` and applies all migrations (`migrations/shared/` THEN `migrations/local-only/`) via `scripts/migrate.py`. WAL mode and FK enforcement are enabled by the inlined connection helper inside `scripts/migrate.py`.

Add Atelier's working directories to `.git/info/exclude` (not `.gitignore`) so they stay out of the project repo:

```
.ai/
lessons/
```

Unlike `.gitignore`, `.git/info/exclude` lives only on your machine and is never committed — the project repo stays completely unaware of Atelier. Verify with `git status`: no output means the directories are invisible to git.

Install runtime dependencies once per machine: `pip install -r requirements.txt`.

## Migration

When Memex becomes available on a machine that has been running Atelier locally, the next Atelier command in a project containing `.ai/atelier.db` will prompt:

```
Memex v2 detected. Migrate this project's Atelier data?  [y/N]
```

- **y** → `scripts/migrate_to_memex.py` replays every row through Memex, archives the local DB as `.ai/atelier-pre-migration-<ts>.db`, and drops `.ai/atelier.migrated` as the success marker.
- **N** → drops `.ai/atelier.local-only`. Atelier keeps using the local backend for this project even though Memex is available. Delete the marker to re-enable the prompt.

Both markers are JSON sidecar files inside `.ai/`. They are consulted by `atelier_entrypoint.startup_check()` on every invocation.

## Scripts

All deterministic operations live in `scripts/`. Each script is callable from the CLI:

| Script | Purpose |
|---|---|
| `scripts/atelier_entrypoint.py` | `startup_check()` for user-facing skills — detects mode, runs bootstrap, prompts for migration |
| `scripts/backend.py` | Mode-dispatched persistence facade — every other module routes through here |
| `scripts/backend_memex.py` | Memex-mode implementations (Core store CRUD via `_memex_core_*`) |
| `scripts/backend_local.py` | Local-mode implementations (SQLite via `_conn()`) |
| `scripts/mode_detector.py` | Detect + cache mode for the current process |
| `scripts/bootstrap.py` | Memex-mode bootstrap (idempotent) |
| `scripts/migrate.py` | Apply SQL migrations to a SQLite file (used by Local mode + bootstrap); inlines DB connection with WAL + FK pragma |
| `scripts/migrate_to_memex.py` | Per-project Local→Memex replay |
| `scripts/session.py` | Session CRUD against the `sessions` table (via the backend facade) |
| `scripts/roles.py` | Role CRUD |
| `scripts/agents.py` | Agent CRUD |
| `scripts/projects.py` | Project CRUD + phase tracking |
| `scripts/documents.py` | Project document CRUD |
| `scripts/tasks.py` | Task CRUD + assign/claim/complete flow |
| `scripts/meetings.py` | Meeting CRUD + `.ai/meetings/*.md` file write |
| `scripts/workflow.py` | Phase gate check (advisory, returns `GateResult`) + transition validation + bypass logging |
| `scripts/workspace.py` | tmux session/window/pane management via libtmux |

Business modules (`projects.py`, `tasks.py`, `meetings.py`, etc.) are now thin wrappers around `backend.*`; they no longer talk to SQLite directly.

## Skills and procedures (two-directory split)

Atelier ships two kinds of agent-facing markdown:

| Location | Discoverable by Claude Code? | Audience | Count |
|---|---|---|---|
| `skills/<name>/SKILL.md` | Yes — exposed as `/atelier:<name>` slash commands | Humans + Claude | 5 (`ingest`, `load`, `migrate`, `run`, `save`) |
| `internal/<name>/SKILL.md` | No — plain markdown procedure files | Agents/subagents only, reached by reading the file | ~27 (dev-*, CRUD, bootstrap-memex, migrate-local-to-memex, self-improve) |

Public skills wrap session lifecycle, the methodology entry, and the local→Memex migration flow. Internal procedures hold the dev arc (design → plan → tdd → review → security → qa → handoff), project DB CRUD, and the mode-specific bootstrap/migration procedures. Agents reach internal procedures by reading the file via the Read tool, then following the steps inline — `run` is the routing index that tells the agent which internal file to read for the current phase.

Each markdown file is a thin wrapper that invokes a Python script in `scripts/` and handles language tasks (grilling, summarizing, generating documents).

## Working rules

1. **Mode is never configured by hand.** Every command starts with `scripts.atelier_entrypoint.startup_check()` and routes through `backend.py`. Never call `backend_memex.*` or `backend_local.*` directly from a skill or test fixture.
2. **Python scripts do the work.** Skill files handle only irreducible language tasks. Do not re-implement logic that belongs in a script.
3. **Phase gates are advisory.** `workflow.py:check_gate` returns a `GateResult`; it does NOT raise on phase mismatch. When `allowed=False`, follow the bypass-confirm-log flow documented in `skills/run/SKILL.md` (Bypass procedure). `workflow.py:advance_phase` still validates the transition graph and DOES raise `WorkflowError` on invalid transitions.
4. **WAL mode + FK enforcement are required.** All Local-mode DB connections go through `scripts.backend_local._conn()` (which the inlined helper in `scripts/migrate.py` mirrors during bootstrap). Memex-mode persistence goes through `scripts.backend_memex._memex_core_*`. Never use raw `sqlite3.connect`.
5. **Meetings write two places.** `meetings.py` writes both a DB record (via `backend`) and `.ai/meetings/YYYY-MM-DD-<slug>.md`. Both must stay in sync.

## DB path convention

| Mode | Path |
|---|---|
| Memex | `~/.memex/atelier.db` (Memex Core store; resolved via `memex:core:resolve-store`) |
| Local | `<project-root>/.ai/atelier.db` |

Scripts that touch SQLite directly (only `migrate.py` and the Local backend) accept a `db_path` positional argument. Everything else resolves the path through `backend.py`.

## Auto-trigger architecture

Atelier's methodology lives in a single canonical file (`skills/run/SKILL.md`) and is surfaced through four mechanisms:

1. **SessionStart hook** (`hooks/session_start.py`) — injects the canonical body as system context every session.
2. **`session_open.py` extension** — appends phase-specific guidance after the existing phase announcement.
3. **CLAUDE.md template snippet** (`templates/CLAUDE-snippet.md`) — short backup methodology for consumer projects.
4. **YAML frontmatter** on the five session-lifecycle skills (`ingest`, `load`, `migrate`, `run`, `save`).

When the methodology changes, edit only `skills/run/SKILL.md`. The hooks parse this file on every invocation, so changes propagate without redeployment. The CLAUDE.md snippet is the only mechanism that requires manual sync; keep it minimal.

## Soft walls

Phase gates (in the `skill_gates` table) are advisory, not enforced. `workflow.py check_gate` returns a `GateResult` describing whether the current phase satisfies the gate; it never raises on phase mismatch (it CAN raise `WorkflowError` for unknown `project_id` — a programming error, not a soft-wall concern). Skills are responsible for the bypass-confirm-log flow when `allowed=False`. Bypasses are recorded in `phase_bypasses` and surfaced by `internal/dev-handoff/SKILL.md` retros.

Hard rule: **never reintroduce raising in `check_gate` for phase mismatch.** If a downstream change makes the soft-wall flow feel insufficient, fix it at the policy layer (the `run` bypass procedure), not by re-walling the gate.

## Skill frontmatter convention

Every public skill at `skills/<name>/SKILL.md` MUST carry YAML frontmatter with a `description` (the routing trigger contract for Claude Code's plugin marketplace):

```yaml
---
description: Use when <trigger condition> — <effect summary>.
---
```

Do NOT include a `name:` field. Per Anthropic's plugin docs, Claude Code automatically derives the slash command as `/<plugin-name>:<dir-name>` from `.claude-plugin/plugin.json`'s `name` field plus the skill's directory name.

Internal procedure files at `internal/<name>/SKILL.md` may keep frontmatter (description) for documentation but it is not read by Claude Code (these files are not registered as plugin skills). Agents reach them by reading the file directly.

### Adding a new procedure

1. Decide whether it's a public slash command or an internal procedure invoked by other skills:
   - **Public:** `skills/<name>/SKILL.md` — appears in `/atelier:` autocomplete. Use for session lifecycle, methodology entry, or commands the human directly types.
   - **Internal:** `internal/<name>/SKILL.md` — invisible to Claude Code's plugin discovery, reached only via Read tool from another skill's procedure. Use for dev workflow steps, mode-specific bootstrap/migration, and CRUD operations.
2. Write the SKILL.md with the description-only frontmatter (or add `user-invocable: false` if it's public-discoverable but Claude-only).
3. If internal, update `skills/run/SKILL.md` to reference the new file in the Phase guidance table or the appropriate routing section.
4. Increment `.claude-plugin/plugin.json`'s `version` field.
5. Re-register in agora afterward: `agora:plugin-register --url https://github.com/nitekeeper/atelier.git`.

## Tests

```bash
PYTHONPATH=. python3 -m pytest tests/
```

Most tests run in Local mode (no Memex install required in CI). The Memex-mode tests use a fake-plugin fixture; the bootstrap end-to-end test is skipped when the real Memex repo is not on disk.

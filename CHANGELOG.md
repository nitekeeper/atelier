# Changelog

## v1.2.0 — 2026-05-20

**Memex-mode bug fixes + bootstrap wiring.** Resolves a class of
`workspace_id`-related crashes that surfaced once atelier started writing
through Memex v2 in production, and wires atelier's bootstrap into
memex's new `ensure_internal_agents()` invariant.

**Memex compatibility:** Best-effort soft dependency on Memex **v2.6.0+**
(for `ensure_internal_agents()`). Older memex versions log a warning and
continue — no crash, no behaviour change beyond the dropped invariant
restore. Memex v2.2.0+ API floor from v1.1.0 is unchanged.

### Added
- `scripts/bootstrap.py:_run_bootstrap_memex` now calls memex's new
  `ensure_internal_agents()` API after seeding atelier's roster into
  `~/.memex/agents.db`, restoring memex's internal-agent invariant
  after each touch of the shared `agents.db`. Soft-imported — older
  memex versions (pre-2.6.0) log a warning and continue. Refs: #9, #12.

### Fixed
- **memex-mode `workspace_id` propagation across 4 write paths.**
  `scripts/projects.py:_resolve_workspace_id` was queried
  unconditionally against `backend_local`, crashing in memex mode with
  `OperationalError: no such table: workspaces`. `backend_memex`'s
  `upsert_session` / `write_document` / `write_meeting` built INSERT
  payloads omitting `workspace_id` while the target tables
  (`sessions`, `project_documents`, `meeting_minutes`) declare
  `workspace_id NOT NULL` — the first memex-mode write to any of these
  would crash with `IntegrityError`. All four paths are now
  mode-aware and inject `workspace_id` correctly. Refs: #6 bugs 1–2
  (+ latent 3–4), #8.

### Internal
- Extracted shared `workspace_resolution` module to eliminate the
  duplicated `_resolve_singleton_workspace_id` helpers that PR #8
  introduced across four scripts. Refs: #10, #11.
- `_atelier_version()` fallback sentinel bumped `1.1.0` → `1.2.0`
  (the v1.1.1 release missed this bump; caught up here).

### Migration notes
- None — pure bug fixes; existing API surface unchanged. Memex
  callers benefit automatically once memex is upgraded to v2.6.0+.

## v1.1.1 — 2026-05-18

### Fixed
- `_scripts_db_shim` no longer recurses into `_load_memex_module`
  when memex imports back into `scripts.*` during bootstrap — the
  shim's reentrancy guard now short-circuits on second entry.
- Documentation and lint guard: replaced bare `python` invocations
  with `python3` in docs; added a lint guard to keep them out of
  future docs.
- Replaced `try/except/pass` with `contextlib.suppress` per ruff
  SIM105.

## v1.1.0 — 2026-05-18

**Memex v2 integration.** Atelier now writes through Memex v2 when
installed, with a slim project-local fallback otherwise.

**Memex compatibility:** Requires Memex **v2.2.0+** (API floor —
caller-built `librarian_output` landed in v2.2.0). Strongly recommended:
**v2.5.0+** (auto-bootstrap eliminates manual `python -m scripts.install`),
**v2.5.1+** (atelier can drop client-side `__*` namespace filtering).
Bootstrap refuses to run against Memex installs older than v2.2.0.

**Typed exceptions surfaced by memex.** Atelier callers may now see the
following typed exceptions propagated from memex:

- `librarian.DuplicateKeyError` — raised on key collision during
  `write_entry` (memex v2.3.0). Atelier's migration replay handles this
  via a client-side Index lookup before every write.
- `embeddings.EmbeddingUnavailable` — raised when embeddings can't be
  produced (oversized input, missing API key, provider error) (memex
  v2.4.1). Atelier surfaces the reason and falls back to FTS-only.
- `data_steward.OrphanNotFoundError` — raised when attempting to operate
  on an `index_id` that isn't present in the documents table (memex
  v2.4.0).
- `db.MemexNotInitializedError` — raised when `~/.memex/registry.json`
  is missing (memex v2.5.0). Atelier's `migrate_to_memex` catches and
  re-raises with operator guidance ("Run `memex:run` once before
  migrating").
- `db.MemexHomeInvalidError` — raised when `MEMEX_HOME` is set to an
  invalid path (memex v2.5.0).

### Added
- Dual-mode persistence facade (`scripts/backend.py`) — auto-selects
  between Memex Core and project-local SQLite.
- `scripts/backend_memex.py` — Tier 2 writes through
  `librarian.write_entry()` with caller-built `librarian_output` (no LLM
  dispatch for Atelier's structured domains); Tier 1 state mutations via
  Memex Core direct.
- `scripts/backend_local.py` — slim SQLite with FTS5 over a local
  `documents` table; raw bodies archived to `.ai/raw/`.
- `scripts/bootstrap.py` — idempotent Memex-mode bootstrap (seeds
  Atelier roles + shipped agents into `~/.memex/agents.db`; creates
  the `atelier` store; enforces Memex v2.2.0+ API floor; piggybacks
  on memex v2.5.0+ auto-bootstrap when available).
- `scripts/migrate_to_memex.py` — one-shot per-project replay from
  Local to Memex; crash-safe (no marker without full success).
- `scripts/atelier_entrypoint.py:startup_check()` — pre-flight for the
  four pre-existing user-facing skills (load, save, ingest, run); handles
  bootstrap + migration prompt. `/atelier:migrate` is excluded from
  pre-flight to avoid circular logic (it IS the migration path).
- `scripts/domain_vocabulary.py` — fixed Atelier domain set
  (`project` / `task` / `meeting` / `project_doc` / `adr`); validated
  on every Tier 2 write.
- `templates/roles.json` + `templates/agents/*.json` — Atelier-shipped
  role + agent seed data, used by both modes.
- `migrations/shared/` + `migrations/local-only/` — split so Memex mode
  consumes only schema-without-roles-or-agents (Memex's agents.db
  owns those tables). `migrations/shared/006_index_ids.sql` adds
  `index_id` columns required by `librarian.write_entry`.
- 8 new internal procedures under `internal/{memex,local,bootstrap-memex,
  migrate-local-to-memex}/` plus `internal/memex/domain-vocabulary.md`.

### Changed
- `scripts/{projects,tasks,documents,meetings,session,workflow,roles,
  agents}.py` rewired to call `backend.*` instead of opening SQLite
  directly. Public signatures unchanged.
- `CLAUDE.md` no longer requires Memex to be installed.

### Removed
- `scripts/db.py` — module's only consumer (the connection helper) is
  now inline in `scripts/migrate.py`.
- `.ai/memex.db` hard-dependency check.

## v0.2.0 — 2026-05-15

### Added
- `skills/using-atelier/SKILL.md` — canonical methodology source (trigger contract, Red Flags, phase guidance, dev arc, bypass procedure).
- `hooks/session_start.py` — SessionStart hook injecting `using-atelier` body as system context.
- `templates/CLAUDE-snippet.md` — backup methodology snippet for consumer projects' CLAUDE.md.
- `phase_bypasses` table and `workflow.py log-bypass` CLI subcommand for auditing soft-wall bypasses.
- YAML frontmatter with `description: Use when…` on `using-atelier`, `ingest`, `save`, `load`.
- `dev:handoff` retro now surfaces phase bypasses from `phase_bypasses` table.
- Migration `005_soft_walls.sql`.

### Changed
- `workflow.py check_gate` now returns a `GateResult` dataclass instead of raising `WorkflowError` on phase mismatch. Out-of-phase invocations no longer block — skills ask the user to confirm a bypass.
- CLI `workflow.py check-gate` now outputs JSON (`{"allowed", "current_phase", "required_phase", "reason"}`) and always exits 0. **Breaking change**: scripts using shell exit code to detect gate-not-met must migrate to parsing the JSON `allowed` field.
- All dev skills' (`dev:design`, `dev:plan`, `dev:tdd`, `dev:review`, `dev:security`, `dev:qa`, `dev:diagnose`, `dev:handoff`) step 1 updated for the new JSON-based `check-gate` contract and (where walled) the bypass flow. Note: skills shipped as one file per concern (`dev:tdd` is one skill handling all three TDD states red/green/refactor, etc.) — the v0.1.0 CHANGELOG entry listing them separately reflected an earlier naming intent.
- `hooks/session_open.py` now appends phase-specific guidance derived from `using-atelier/SKILL.md`'s phase guidance table.

### Deprecated
- `WorkflowError` raise behavior in `check_gate` on phase mismatch. The exception class itself remains for `workflow.py advance` invalid-transition errors and for `check_gate` invalid-project-id errors.

### Migration notes
- Run `python scripts/migrate.py .ai/memex.db` to apply migration 005.
- **Note:** Atelier scripts currently default to two different DB paths (`workflow.py`/`session.py` use `.ai/atelier.db`; CRUD scripts and `migrate.py` use `.ai/memex.db`). This inconsistency predates v0.2.0 and is tracked as a follow-up cleanup. Ensure both paths are migrated if your project has both.
- Install the SessionStart hook per README "Auto-trigger setup" section.
- (Optional) paste `templates/CLAUDE-snippet.md` into your project's `CLAUDE.md`.

## Unreleased

### Added
- `dev:self-improve` skill — autonomous multi-agent improvement cycle with isolated git clone, unanimous consensus gate, destructive-change detection, and full test gate before merge
- `scripts/destructive_check.py` — detects destructive changes in git diffs (5 categories: deleted imported files, removed public functions, destructive DB migrations, removed skill directories, removed test files)
- `scripts/self_improve.py` — git infrastructure for self-improve cycles (clone, branch, test, commit, push-merge, cleanup, pull)

## v0.1.1 — 2026-05-12

### Fixed

- `migrations/` directory now included in `dist/` — was missing from v0.1.0, causing `migrate.py` to silently skip all SQL migrations when run from `dist/`

## v0.1.0 — 2026-05-12

### Added

- **Foundation** — SQLite database, migration runner, session management, shared pytest conftest
- **Coordination layer** — `scripts/roles.py`, `scripts/projects.py`, `scripts/tasks.py` with full CRUD and search; `skills/role`, `skills/project`, `skills/task`, `skills/meeting`, `skills/doc`, `skills/ingest`, `skills/load`, `skills/save` (22 SKILL.md files total)
- **Workspace layer** — `scripts/workspace.py` for tmux session management (workspaces, rooms, agent desks); `skills/workspace`, `skills/room`, `skills/agent-desk`, `skills/agent`
- **Dev workflow** — `scripts/workflow.py` with phase state machine (design → approved → in-progress → code-review → done / diagnose); `skills/dev-design`, `skills/dev-plan`, `skills/dev-tdd-red`, `skills/dev-tdd-green`, `skills/dev-tdd-refactor`, `skills/dev-code-review`, `skills/dev-qa-review`, `skills/dev-security-review`, `skills/dev-handoff`, `skills/dev-diagnose`
- **Cross-platform workspace** — `scripts/preflight.py` with platform detection, WSL check, and tmux auto-install; Windows routes workspace commands through WSL subprocess (`wsl -- tmux ...`); macOS/Linux use libtmux directly
- **108 tests** across all modules; `tests/conftest.py` for safe CI imports

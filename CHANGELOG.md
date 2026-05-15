# Changelog

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

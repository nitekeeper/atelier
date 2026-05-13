# Changelog

## Unreleased

## v0.1.0 — 2026-05-12

### Added

- **Foundation** — SQLite database, migration runner, session management, shared pytest conftest
- **Coordination layer** — `scripts/roles.py`, `scripts/projects.py`, `scripts/tasks.py` with full CRUD and search; `skills/role`, `skills/project`, `skills/task`, `skills/meeting`, `skills/doc`, `skills/ingest`, `skills/load`, `skills/save` (22 SKILL.md files total)
- **Workspace layer** — `scripts/workspace.py` for tmux session management (workspaces, rooms, agent desks); `skills/workspace`, `skills/room`, `skills/agent-desk`, `skills/agent`
- **Dev workflow** — `scripts/workflow.py` with phase state machine (design → approved → in-progress → code-review → done / diagnose); `skills/dev-design`, `skills/dev-plan`, `skills/dev-tdd-red`, `skills/dev-tdd-green`, `skills/dev-tdd-refactor`, `skills/dev-code-review`, `skills/dev-qa-review`, `skills/dev-security-review`, `skills/dev-handoff`, `skills/dev-diagnose`
- **Cross-platform workspace** — `scripts/preflight.py` with platform detection, WSL check, and tmux auto-install; Windows routes workspace commands through WSL subprocess (`wsl -- tmux ...`); macOS/Linux use libtmux directly
- **108 tests** across all modules; `tests/conftest.py` for safe CI imports

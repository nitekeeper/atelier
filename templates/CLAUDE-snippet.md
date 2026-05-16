<!-- Atelier methodology — paste into your project's CLAUDE.md -->
<!-- For full reference, see skills/using-atelier/SKILL.md in your Atelier install. -->

## Atelier methodology

This project uses Atelier for development workflow.

**On every user message, before responding:**

1. **Mid-arc rule.** If a project is active and its phase is not `handoff:complete`, continue the current arc. Do NOT ask. Use the phase-recommended skill from `using-atelier/SKILL.md`.
2. **No-fire rule.** Questions, exploration, read-only requests, and trivial edits are handled directly without asking.
3. **Ask gate.** New development work triggers a three-routing ask:
   - **(a) Full Atelier arc** — `internal/project/SKILL.md` (`create`) then `internal/dev-design/SKILL.md` → plan → tdd → review → security → qa → handoff (See `using-atelier/SKILL.md` for the authoritative phase sequence and per-phase guidance.)
   - **(b) Bug fix** — `internal/dev-diagnose/SKILL.md` (captures pre-diagnose phase, restores on resolve)
   - **(c) Handle directly** — no project, no phase tracking

**Soft walls.** Phase gates are recommendations, not blocks. When a dev skill detects an out-of-phase invocation, it asks the user to confirm a bypass, then logs the bypass to `phase_bypasses` for retrospective.

**Full methodology:** `skills/using-atelier/SKILL.md` in the Atelier install.
**Phase state:** `.ai/atelier.db` `projects.phase`.
**Phase gates:** `python atelier/scripts/workflow.py <db_path> check-gate <project_id> <skill>` (returns JSON; never blocks).

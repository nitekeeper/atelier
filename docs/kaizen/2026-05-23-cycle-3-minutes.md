# Kaizen Run 4 Cycle 1 Meeting — atelier

**Date:** 2026-05-23 00:48 UTC
**Facilitator:** Dr. Priya Nair (PM)
**Status:** consensus reached after a major safety finding redirected scope; 5 Action Items approved unanimously

## Participants

| Agent | Role | Phase 2 participation |
|---|---|---|
| Dr. Samuel Okafor | Software Engineer (Backend) | active |
| Dr. Fatima Al-Rashid | AI Safety Researcher | active |
| Dr. Nadia Petrov | Agent Systems Architect | active |

## PM Assessment

The cycle opened to **clear two TODO breadcrumbs left in PRs atelier#20 and #21** — `<!-- TODO: route via backend facade once list_phase_bypasses lands -->` in `internal/dev-handoff/SKILL.md` and `internal/dev-finish/SKILL.md`. The plan was to add `list_phase_bypasses` to `scripts/backend.py`, route the SKILL.md workaround code through it, and eliminate the CLAUDE.md rule 1 violation ("Never call `backend_memex.*` or `backend_local.*` directly from a skill").

Phase 2 surfaced **a much more serious finding**: the workaround SQL in BOTH SKILL.md files references v1.0.13 schema columns (`skill`, `current_phase`, `required_phase`, `note`) that **no longer exist in the v1.1.0 `phase_bypasses` table** (which has `from_phase`, `to_phase`, `reason`, `agent_id`). This SQL would crash with `sqlite3.OperationalError: no such column: skill` at runtime. The workaround code shipped in PRs #20 and #21 was never executable; the audit-trail retro has been silently absent since the v1.0.13 → v1.1.0 schema migration.

Backend, Safety, and Architect independently confirmed the schema mismatch by reading `migrations/shared/001_v110_schema.sql:200-208` and the v1.1.0-column comment in `tests/test_phase_bypasses.py:4-7`. Safety also confirmed that `tests/test_handoff_with_bypasses.py:16` asserts `"by skill"` in the rendered retro text — an assertion that has been silently asserting against unreachable rendering code.

## Discussion

**Backend Engineer (Dr. Okafor)**: proposed Option B (facade returns pre-aggregated `GROUP BY` rows with optional `GROUP_CONCAT(reason)`), arguing it removes Python aggregation from two callers.

**Architect (Dr. Petrov) rebuttal**: backend.py's `list_*` methods (`list_tasks`, `list_projects`, `list_workspaces`) all return raw rows. Pre-aggregation in the facade would create a unique return shape no other method shares, harder to mock uniformly. Recommend Option A (raw rows); aggregation belongs in the skill's rendering layer.

**Safety (Dr. Al-Rashid) reinforcement**: `GROUP_CONCAT` is SQLite-specific. `_memex_core_query` in `backend_memex.py:974-986` uses a generic `SELECT * FROM <table> WHERE …` pattern; a custom aggregation query would have to bypass `_memex_core_query` entirely and call `_memex_module("stores").query()` directly — exposing the same API-surface inconsistency the facade is supposed to fix. **Hard-stop**: Option B reintroduces the architectural smell.

**PM ruling**: Option A wins 2-1 + safety hard-stop. Raw rows; callers aggregate in Python at render time.

**Safety F4**: there is currently **no test** that asserts `list_phase_bypasses` correctly scopes by `project_id`. A WHERE-clause omission would silently surface cross-project bypasses. **Hard-stop**: add a parametrised cross-project filter test.

**Safety F5**: removing the workaround is **strictly superior** — the workaround is unreachable code today. Nothing is lost.

**Safety F6**: grep confirmed only 2 files violate CLAUDE.md rule 1 via direct backend access (`dev-handoff`, `dev-finish`). No additional cleanup needed this cycle.

**Architect** also flagged: `tests/test_handoff_with_bypasses.py:16` will need updating once the dev-handoff retro format string is corrected to use v1.1.0 column names.

## Decisions Log

- **D1.** Add `list_phase_bypasses(*, project_id: int) -> list[dict]` to the facade returning **raw rows** (Option A). Unanimous.
- **D2.** Use v1.1.0 column names everywhere — facade SQL, SKILL.md rendering, test assertions. Unanimous.
- **D3.** Test plan must include a cross-project filter assertion (Safety F4). Unanimous.
- **D4.** Replace the workaround code blocks in `dev-handoff` + `dev-finish` with a single facade call each; aggregate for display in Python. Remove both TODO breadcrumb comments. Unanimous.
- **D5.** Update `tests/test_handoff_with_bypasses.py:16` to assert the v1.1.0 rendered text (e.g. `"from <from_phase> → <to_phase>"`) instead of the dead `"by skill"`. Unanimous.
- **D6.** Risk classification: **NON-DESTRUCTIVE in effect** — the workaround code being removed is unreachable today (would crash on runtime if executed), so deletion is value-preserving. `destructive_check.py` only inspects `.py` removals (per kaizen task 16) and these deletions are `.md` only. Unanimous.

## Action Items

| # | Action | Files |
|---|---|---|
| AI-1 | Add `list_phase_bypasses` to facade + both backends (raw rows, kw-only signature) | `scripts/backend.py`, `scripts/backend_local.py`, `scripts/backend_memex.py` |
| AI-2 | Add tests: empty case, cross-project filter (Safety F4), ordering. Add to `test_backend_dispatch.py` symmetry test. | `tests/test_backend_local_state.py`, `tests/test_backend_memex_state.py`, `tests/test_backend_dispatch.py` |
| AI-3 | Update `dev-handoff/SKILL.md` step 4: replace workaround block (lines ~49-95) with single facade call + Python aggregation using v1.1.0 columns. Remove TODO + NOTE comments. | `internal/dev-handoff/SKILL.md` |
| AI-4 | Same for `dev-finish/SKILL.md` step 6 (~lines 79-125): facade call, simpler render (no notes column). Remove TODO. | `internal/dev-finish/SKILL.md` |
| AI-5 | Fix `tests/test_handoff_with_bypasses.py:16` assertion: replace `"by skill"` with the new v1.1.0 rendering format substring. | `tests/test_handoff_with_bypasses.py` |

**Total files touched:** 7 + commit-cycle minutes file. Code + tests + 2 SKILL.md + 1 test fix.

## Cycle outcome

Status: PROCEED to Phase 4.
Approved Action Items: 5.
Risk: NON-DESTRUCTIVE in effect (deleting unreachable code).

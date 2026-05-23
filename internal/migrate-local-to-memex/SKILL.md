---
description: Internal — one-shot per-project migration from Local-mode atelier.db to machine-global Memex. Called only when mode_detector returns memex AND should_prompt returns True.
---

# migrate-local-to-memex (internal)

> **Prerequisites**
> - Mode: **MEMEX ONLY** — Memex v2 must be installed and bootstrapped (`~/.memex/registry.json` must exist); `mode_detector.detect_mode()` must return `"memex"` and `migrate_to_memex.should_prompt()` must return `True`
> - Required: a project running in Local mode with `.ai/atelier.db` present (i.e. neither `.ai/atelier.migrated` nor `.ai/atelier.local-only` marker is present)
> - Required tables: reads from `<project>/.ai/atelier.db` (tables: `projects`, `tasks`, `meetings`, `sessions`, `phase_bypasses`, `project_documents`); writes to `~/.memex/atelier.db` (Memex Core store) via `backend_memex.write_*`

## Trigger

At the top of any Atelier user-facing skill in Memex mode, before any
real work: check `scripts.migrate_to_memex.should_prompt(<project>/.ai)`.
If True, follow the recipe below. If False, proceed with the original
command.

## Recipe

0. Verify Memex is bootstrapped. `migrate_project` internally calls
   `backend_memex.require_memex_bootstrap()` and raises a `RuntimeError`
   with operator guidance if `~/.memex/registry.json` is missing. If you
   see that error, instruct the user: "Run `memex:run` once before
   migrating", then abort the recipe.

1. Call `migrate_to_memex.row_summary(local_db)` to get a per-table count.

2. Present to the user:

   ```
   Memex v2 detected. Atelier currently has local data at .ai/atelier.db:
     - <N> projects
     - <N> tasks
     - <N> meeting minutes
     - <N> sessions
     - <N> phase bypasses
     - <N> project documents

   Migrate to Memex now?  [y/N]
   ```

3. On `y`: call `migrate_to_memex.migrate_project(local_db)`. Report
   the returned summary (per-table `migrated` and `already_present`
   counts plus the archive filename) to the user, then continue with
   the original command.

4. On `N`: call `migrate_to_memex.decline_migration(<project>/.ai)`.
   Continue in Local mode for this project.

## Failure semantics

`migrate_project` is non-destructive on failure. If any
`backend_memex.write_*` call raises:

- The `.migrated` marker is NOT written.
- The local `atelier.db` is NOT renamed.
- The exception propagates to the caller.

The next Atelier command will see `should_prompt` return True again
and the user can retry. Re-runs are safe — each replayed row is
checked against the Memex Index by `source_ref` (`atelier:<table>:<id>`)
before writing, so rows that already landed during the partial run
count toward `already_present` and are skipped.

## Re-entry semantics

- After successful migration the `.ai/atelier.migrated` marker prevents
  re-prompt.
- After decline the `.ai/atelier.local-only` marker prevents re-prompt.
- The user can delete either marker to re-trigger the recipe.

## Implementation entry points

| Function | Purpose |
|---|---|
| `migrate_to_memex.should_prompt(ai_dir)` | True iff `atelier.db` exists and neither marker is present. |
| `migrate_to_memex.row_summary(local_db)` | Per-table row counts for the prompt UI. |
| `migrate_to_memex.migrate_project(local_db)` | Replay all rows; rename DB; write marker. |
| `migrate_to_memex.decline_migration(ai_dir)` | Write the `.local-only` marker. |

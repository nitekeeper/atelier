---
description: Use when manually triggering or re-triggering Local → Memex migration for the current Atelier project — bypasses the .ai/atelier.local-only opt-out marker and retries on prior failure.
---

# migrate

Manual trigger for the Local → Memex migration documented in spec §8 and
implemented in `internal/migrate-local-to-memex/SKILL.md`. The same
migration logic auto-prompts at the top of `/atelier:{load,save,ingest,run}`
when both a project-local `.ai/atelier.db` and Memex are present and no
marker has been written. This skill is the **manual** path for the
exception cases.

## When to use

- You previously answered `n` to the auto-prompt and now want to migrate.
  (Auto-prompt won't fire again because `.ai/atelier.local-only` is set.)
- A prior migration failed partway through. You've resolved the cause
  (disk full, Memex bootstrap issue, etc.) and want to resume.
- You're scripting bulk migration across many projects: invoke
  `/atelier:migrate` from each project root rather than waiting for
  the auto-prompt at next session-open.

## Procedure

1. **Verify mode.** Run `from scripts.mode_detector import detect_mode;
   detect_mode()`. If it returns `"local"`, surface to user:
   "Memex is not installed (or not bootstrapped). Run `memex:run` once
   first, then re-invoke `/atelier:migrate`." Stop.

2. **Clear opt-out marker if present.** If `.ai/atelier.local-only`
   exists, delete it. (Whether the user explicitly opt-out is no longer
   relevant — they're explicitly opting back in now.)

3. **Verify there's something to migrate.** If `.ai/atelier.db` doesn't
   exist, surface: "No local atelier database in this project. Nothing
   to migrate." Stop.

4. **Run the internal migration procedure.** Read
   `internal/migrate-local-to-memex/SKILL.md` and follow it inline.
   The procedure is idempotent — rows already replayed (detected via
   `source_ref` lookup in memex's Index, per Plan 4 Task 1) are skipped
   and counted under `already_present`. Surface the summary to the user.

5. **On success**, the procedure writes `.ai/atelier.migrated` with the
   timestamp + row counts. On failure, the local DB is untouched and no
   marker is written; the user can fix the underlying issue and re-run
   this skill.

## Differences from the auto-prompt path

| Aspect | Auto-prompt (Task 2) | `/atelier:migrate` (this skill) |
|---|---|---|
| When triggered | Top of any other skill, on first session-open after Memex appears | User-invoked explicitly |
| Respects `.ai/atelier.local-only` | Yes — won't fire if marker present | **No** — clears the marker |
| Respects `.ai/atelier.migrated` | Yes — won't fire if marker present | **Same** — but the internal procedure detects already-migrated rows via source_ref and counts them rather than re-writing |
| Required answer | `y/N` from user | None — invocation IS the consent |

## Hard rules

- Never proceed if `detect_mode() != "memex"`. Atelier cannot migrate
  TO a target that doesn't exist.
- Never skip the internal procedure — call into
  `internal/migrate-local-to-memex/SKILL.md` directly so all replay
  logic, source_ref checks, and crash-safety guarantees are honored.
- Surface the row-count summary to the user before stopping. Do not
  swallow the migration's output.

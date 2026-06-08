---
description: Use when capturing a document, URL, decision, or learned context into the shared Memex wiki for future sessions.
---

# ingest

Capture knowledge into the shared Memex wiki. Any actor — human or agent — can call this.

## When to use

Call `ingest` whenever you have knowledge worth preserving:
- A document or file the user has provided
- A URL with relevant project information
- Something learned during this session about the codebase
- A decision made in a meeting
- Any context that would be expensive to reconstruct later

## Pre-flight (always first)

Run `from scripts.atelier_entrypoint import startup_check; startup_check()`.

Branch on the returned `action`:

- **`proceed-local`** — Memex is not installed. Continue with the rest of
  this skill's recipe; all writes go to the project-local `.ai/atelier.db`.
- **`proceed-memex`** — Memex is installed and bootstrapped. Continue;
  all writes go through Memex.
- **`prompt-migration`** — Memex is installed but this project still
  has a local DB. Read `internal/migrate-local-to-memex/SKILL.md` and
  follow its prompt protocol. After the user answers, restart the
  pre-flight (`startup_check()` will now return `proceed-memex` or
  `proceed-local` depending on the user's choice).

**Settings recommendation (after pre-flight):** if `startup_check()` returned a
`settings_rec_offer` with `eligible=True` and non-empty `changes`, read
`internal/settings-recommendation/SKILL.md` and follow its prompt protocol
BEFORE proceeding to the rest of this skill. After the user's choice, continue
the original command.

## Procedure

1. Ask the user (or calling agent): "What do you want to capture? Provide the content, file path, or URL."

1b. **Verify Memex CLI availability.**
   Before invoking `capture`, run:
   ```
   memex --version
   ```
   - If the command exits 0: continue to step 2.
   - If the command is not found (`command not found` / `is not recognized`): stop. Tell the user: "The `memex` CLI is not on PATH. Either activate the environment where Memex is installed, or provide a full path to the `memex` executable. Install: `pip install memex`." Do not fall back to writing the content elsewhere — session knowledge written outside Memex is invisible to future agents.

2. Invoke Memex `capture` with the provided input.
3. Follow Memex `capture`'s approval flow — do not write to the wiki without approval.
4. Confirm: "Captured to Memex wiki."

## Hard rules

- Never skip the Memex `capture` approval gate.
- Never invent or summarize content — capture what was provided, not your interpretation of it.
- If the input is a URL, fetch it first and confirm the content before capturing.

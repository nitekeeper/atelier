# load

Session-open command. Reads current task state and surfaces relevant knowledge from Memex before work begins.

## When to use

Call `load` at the start of every session. This is the first thing you do.

## Procedure

1. Run: `python atelier/scripts/session.py read .ai/work.md`
2. If session state exists, announce to the user:
   > "Resuming: [current-task]. Status: [status]. Last session: [last-session]. Next action: [next-action]."
   If no session state exists, announce:
   > "No previous session found. Starting fresh."
3. If session state exists, run two Memex `ask` queries:
   - Query 1: the value of `current-task` from `.ai/work.md`
   - Query 2: the files or area being worked on (ask the user if not clear from task)
4. Present the Memex results as context before proceeding.
5. Begin work from `next-action`.

## Hard rules

- Always announce session state before doing anything else.
- Always run both Memex `ask` queries when session state exists — never skip them.
- Do not begin work until steps 1–4 are complete.

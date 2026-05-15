---
name: atelier:load
description: Use when resuming work mid-arc and needing the latest session context loaded into the conversation.
---

# load

Session-open command. Reads the most recent session from the DB and surfaces relevant knowledge from Memex before work begins.

## When to use

Call `load` at the start of every session, before any other action.

## Procedure

1. **Identify the active project.**
   Run (from the target project root):
   ```
   python atelier/scripts/projects.py list
   ```
   (DB: `.ai/memex.db`)

   - If **exactly one** project exists: use it automatically. Announce: "Found project: **[name]** (ID [id], phase: [phase])."
   - If **multiple** exist: display the list (id, name, phase) and ask the user: "Which project are you resuming?" Wait for a response, then confirm: "Resuming **[name]** (ID [id], phase: [phase]). Correct? (yes/no)"
   - If **none** exist: announce "No projects found. Run `project:create` to set up a project first." Stop.

2. **Read the latest session.**
   Run:
   ```
   python atelier/scripts/session.py read-latest <project_id>
   ```
   (DB: `.ai/memex.db`)

3. **Announce session state.**
   If a session row was returned, announce:
   > "Resuming: [current_tasks]. Status: [status]. Last closed: [closed_at]. Next action: [next_action]."

   If `closed_at` is null, omit the "Last closed" clause:
   > "Resuming: [current_tasks]. Status: [status]. Next action: [next_action]."

   If `blocking_reason` is non-null, append:
   > "Blocked: [blocking_reason]."

   If the command returns "No session found for this project.", announce:
   > "No previous session found for this project. Starting fresh."

4. **Surface Memex context** (only if a session row was returned).
   Run two Memex `ask` queries:
   - Query 1: the value of `current_tasks` from the session record
   - Query 2: the value of `next_action` from the session record (or ask the user which files/area to query if `next_action` is not file-specific)
   Present the results. If Memex returns nothing relevant, say so briefly and proceed.

5. **Confirm next action with the user.**
   Do not begin executing `next_action` automatically. Ask:
   > "Ready to continue from: '[next_action]'? Or do you want to start somewhere else?"
   Begin work only after the user confirms or redirects.

## Hard rules

- Always run steps 1–4 before doing any work.
- Never skip the project-identification step — `read-latest` requires a valid `project_id`.
- Both `projects.py` and `session.py` use `.ai/memex.db`. Pass no db path; the default is correct.
- Never begin executing `next_action` without user confirmation.
- If project identification or session read fails, surface the error to the user before proceeding.

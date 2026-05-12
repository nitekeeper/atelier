# save

Session-close command. Writes current task state to `.ai/work.md` and optionally captures session knowledge to Memex.

## When to use

Call `save` when closing a session. This is the last thing you do before ending work.

## Procedure

1. Run: `python atelier/scripts/session.py write` — the script prompts for:
   - `current-task`: one sentence describing what is in progress
   - `status`: in-progress | blocked | complete
   - `blocking-reason`: (if blocked) what is stopping progress
   - `accomplished`: what was done this session
   - `next-action`: first imperative action for the next session
2. Confirm to the user: "Session state saved to `.ai/work.md`."
3. Ask: "Anything to capture to the knowledge base? (y/n)"
   - If yes: invoke `ingest`
   - If no: done

## Hard rules

- Always run step 1 before asking about ingest — session state is never optional.
- Never close without writing `.ai/work.md`.
- `next-action` must be an imperative sentence (e.g., "Run the migration tests").

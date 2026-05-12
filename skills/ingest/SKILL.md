# ingest

Capture knowledge into the shared Memex wiki. Any actor — human or agent — can call this.

## When to use

Call `ingest` whenever you have knowledge worth preserving:
- A document or file the user has provided
- A URL with relevant project information
- Something learned during this session about the codebase
- A decision made in a meeting
- Any context that would be expensive to reconstruct later

## Procedure

1. Ask the user (or calling agent): "What do you want to capture? Provide the content, file path, or URL."
2. Invoke Memex `capture` with the provided input.
3. Follow Memex `capture`'s approval flow — do not write to the wiki without approval.
4. Confirm: "Captured to Memex wiki."

## Hard rules

- Never skip the Memex `capture` approval gate.
- Never invent or summarize content — capture what was provided, not your interpretation of it.
- If the input is a URL, fetch it first and confirm the content before capturing.

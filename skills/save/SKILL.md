---
name: save
description: Use when ending a session or at a meaningful checkpoint — captures session state for the next resume.
---

# save

Session-close command. Commits git state, writes a session row to the DB, and optionally captures session knowledge to Memex. In a git worktree, also merges back to the base branch and cleans up the worktree.

## When to use

Call `save` when closing a session or at a meaningful checkpoint. This is the last thing you do before ending work.

## Procedure

### Phase A — Git state

1. **Check for uncommitted changes.**
   Run (from the target project root):
   ```
   git status --short
   ```
   - If **clean** (no output): skip to step 2.
   - If **dirty**: ask the user for a brief commit message, then:
     ```
     git add -A
     git commit -m "<message>"
     ```
     If `git commit` fails, surface the error and do not proceed until the working tree is clean.

2. **Detect worktree.**
   Run:
   ```
   git rev-parse --git-dir
   ```
   - If the output is literally `.git`: not a worktree. Skip to Phase B.
   - If the output path contains `worktrees/` (or `worktrees\` on Windows): you are in a linked worktree. Continue to Phase A-WT.

#### Phase A-WT (linked worktree only)

3. **Merge back, remove worktree, delete branch.**
   Run:
   ```
   python atelier/scripts/worktree.py merge-back
   ```
   This script:
   - Verifies the main workspace is on the expected base branch and clean.
   - Merges the worktree branch into the base branch with `--no-ff`.
   - Removes the worktree directory.
   - Deletes the worktree branch.

   If the script exits non-zero (conflict, dirty main, detached HEAD): follow its printed recovery instructions. Do not proceed to Phase B until cleanup is complete or the user explicitly decides to skip cleanup.

### Phase B — Write session

4. **Determine context.**
   You need four values before calling `session.py write`. If you ran `load` this session they are in working memory. If not:
   - `project_id`: run `python atelier/scripts/projects.py list` and select the active project.
   - `agent_id`: run `python atelier/scripts/agents.py list` and identify your own agent record by name. Ask the user if your record is ambiguous or missing.
   - `phase`: read from the project record returned by `projects.py list` (`phase` column).
   - `status`: determine from session state — `in-progress`, `blocked`, or `complete`.

5. **Gather session fields.**
   Draft the following from your working memory of this session, then present as a block for user confirmation:

   ```
   current_tasks  : <one sentence — what is in progress right now>
   accomplished   : <what was completed or meaningfully advanced this session>
   next_action    : <first imperative action for the next session>
   status         : in-progress | blocked | complete
   blocking_reason: <required only if status is "blocked">
   ```

   Ask: "Does this capture the session correctly? (edit or confirm)" Wait for confirmation before writing.

6. **Write the session.**
   Run:
   ```
   python atelier/scripts/session.py write <project_id> <agent_id> <phase> <status> \
     --current-tasks "<current_tasks>" \
     --accomplished "<accomplished>" \
     --next-action "<next_action>" \
     [--blocking-reason "<blocking_reason>"]
   ```
   (DB: `.ai/memex.db`)

   If the command exits non-zero or returns an error: **abort**. Do not confirm success. Surface the error to the user.

   On success, confirm: "Session saved (project [project_id], session [id from output])."

### Phase C — Knowledge capture

7. **Offer Memex ingest.**
   Ask: "Anything to capture to the knowledge base? (y/n)"
   - If yes: invoke `ingest`.
   - If no: done.

## Hard rules

- Never skip Phase A — git state must be clean before writing a session row.
- Always check for worktree (step 2) — leaked worktrees accumulate silently.
- Never proceed past a failed `session.py write` — if the DB write fails, do not confirm success.
- `next_action` must be an imperative sentence (e.g., "Run the migration tests"). Reject vague values like "continue" or "TBD".
- Both `projects.py` and `session.py` use `.ai/memex.db`. Pass no db path; the default is correct.
- Never reference `.ai/work.md` — that file no longer exists in this architecture.

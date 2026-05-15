# Self-Improvement Meeting — Cycle 1
**Date:** 2026-05-15 UTC
**Facilitator:** Dr. Priya Nair (PM)
**Subject:** Fix load/save skills — DB interface and worktree cleanup
**Participants:**
| Agent | Role |
|---|---|
| Dr. Nadia Petrov | Agent Systems Architect |
| Dr. Yusuf Okafor | Prompt Engineer |
| Dr. Fatima Al-Rashid | AI Safety Researcher |
| Dr. Kwame Asante | DevOps/VCS Engineer |
| Dr. Lena Novak | Software Architect |
| Dr. Aisha Mensah | Cognitive Scientist |

## Agenda
1. Fix DB path inconsistency — in-scope or defer?
2. How should `load` resolve `project_id`?
3. Should `save` redirect to `dev:handoff` or be fixed in place?
4. Where does the worktree git sequence live?
5. Merge conflict handling in worktree merge-back
6. `agent_id` resolution in `save`

## Discussion

### Agenda Item 1: Fix DB path inconsistency — in-scope or defer?
**Proposals:**
- Dr. Lena Novak (Software Architect): Minimal fix — change only the three stray scripts (`session.py`, `workflow.py`, `seed_roles.py`) from `.ai/atelier.db` to `.ai/memex.db`.
- Dr. Nadia Petrov (Agent Systems Architect): Move all scripts to `.ai/atelier.db` for consistency.
- Dr. Fatima Al-Rashid (AI Safety Researcher): Agreed with Novak — fixing session.py to `.ai/memex.db` eliminates the root cause.

**Discussion:** The inconsistency is the direct root cause of all load/save failures, not a separate concern. Dr. Petrov's broader direction would conflict with documented setup procedures (CLAUDE.md, migrate.py, README all reference `.ai/memex.db`). Dr. Novak's minimal fix is lower-risk and achieves the same end state.

**Decision:** Change `session.py`, `workflow.py`, `seed_roles.py` CLI defaults from `.ai/atelier.db` to `.ai/memex.db`. — *Unanimous*

### Agenda Item 2: How should `load` resolve `project_id`?
**Proposals:**
- Dr. Nadia Petrov: List projects, auto-select if one, ask if multiple.
- Dr. Fatima Al-Rashid: Add confirmation gate — show project name + phase before reading session.
- Dr. Aisha Mensah: Update announcement template to use `current_tasks`, `next_action`, `closed_at` (old flat-file field names silently produce None).

**Discussion:** All proposals are additive and complementary. No conflicts.

**Decision:** `load` lists projects, auto-selects if exactly one, shows name+phase and asks for confirmation if multiple. Announcement template updated to DB field names. — *Unanimous*

### Agenda Item 3: Should `save` redirect to `dev:handoff` or be fixed in place?
**Proposals:**
- Dr. Nadia Petrov: Deprecate `save`, make it a thin redirect to `dev:handoff`.
- Dr. Yusuf Okafor: Fix in place — `save` is a general checkpoint; `dev:handoff` is project-close with phase gate logic.
- Dr. Aisha Mensah: Supported Okafor — different moments, different purposes.

**Discussion:** `dev:handoff` includes bypass gate, retro, and phase advancement logic. `save` is a lightweight checkpoint. Redirecting would add phase gate friction to mid-session saves.

**Decision:** Fix `save` in place with gather-then-confirm template, non-interactive `write` CLI, explicit db paths. — *Unanimous*

### Agenda Item 4: Where does the worktree git sequence live?
**Proposals:**
- Dr. Kwame Asante: New `scripts/worktree.py` with `merge-back` command, per CLAUDE.md Working Rule 2 ("Python scripts do the work").
- All others: No objection.

**Discussion:** The multi-step detect/commit/merge/cleanup sequence is deterministic logic. CLAUDE.md explicitly prohibits re-implementing this in skill files.

**Decision:** Create `scripts/worktree.py merge-back` to encapsulate the sequence. `save` skill calls `python scripts/worktree.py merge-back`. — *Unanimous*

### Agenda Item 5: Merge conflict handling
**Proposals:**
- Dr. Fatima Al-Rashid: Abort on conflict, leave worktree intact. Deleting the worktree mid-conflict loses the user's context.
- Dr. Kwame Asante: Pre-flight guards (main branch clean, on expected base branch). On conflict: `git merge --abort`, print recovery instructions, halt.

**Discussion:** These proposals are identical in effect. No conflict between agents.

**Decision:** `scripts/worktree.py merge-back` adds pre-flight guards; aborts with `git merge --abort` on conflict; leaves worktree and branch intact; prints manual recovery steps. — *Unanimous*

### Agenda Item 6: `agent_id` resolution in `save`
**Proposals:**
- Dr. Aisha Mensah: No recovery path exists in the skill. Add step: run `python scripts/agents.py list`, identify by name, ask user if ambiguous.
- Dr. Yusuf Okafor: Agreed — no `whoami` command exists.

**Discussion:** This is additive skill text only. No conflicts.

**Decision:** Add `agent_id` recovery step to `save`. — *Unanimous*

## Decisions Log
1. Change `session.py`, `workflow.py`, `seed_roles.py` CLI defaults from `.ai/atelier.db` to `.ai/memex.db` — `scripts/session.py`, `scripts/workflow.py`, `scripts/seed_roles.py`
2. Fix `load` skill: `read-latest <project_id>`, auto-select single project, confirm multi, updated field names — `skills/load/SKILL.md`
3. Fix `save` skill: non-interactive `write` CLI, gather-then-confirm template, explicit db path — `skills/save/SKILL.md`
4. Add `git status --porcelain` check + commit step to `save` (skip if clean) — `skills/save/SKILL.md`
5. Add worktree detect/merge-back/delete via script call — `skills/save/SKILL.md`
6. Create `scripts/worktree.py merge-back` — `scripts/worktree.py`
7. `worktree.py merge-back` aborts on conflict with recovery instructions — `scripts/worktree.py`
8. Add `agent_id` recovery path to `save` — `skills/save/SKILL.md`
9. Confirm project selection on load (show name + phase) — `skills/load/SKILL.md`

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | Fix `db_path` defaults | `scripts/session.py`, `scripts/workflow.py`, `scripts/seed_roles.py` | Dr. Lena Novak |
| 2 | New `worktree.py` with `merge-back` | `scripts/worktree.py` | Dr. Kwame Asante |
| 3 | Rewrite `load` skill | `skills/load/SKILL.md` | Dr. Yusuf Okafor + Dr. Aisha Mensah |
| 4 | Rewrite `save` skill | `skills/save/SKILL.md` | Dr. Yusuf Okafor + Dr. Fatima Al-Rashid |
| 5 | Write tests for `worktree.py` | `tests/test_worktree.py` | Dr. Kwame Asante |

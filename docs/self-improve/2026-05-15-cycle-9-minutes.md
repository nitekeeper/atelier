# Self-Improvement Meeting — Cycle 9
**Date:** 2026-05-15 UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. Nadia Petrov | Agent Systems Architect |
| Dr. Fatima Al-Rashid | AI Safety Researcher |
| Dr. Yusuf Okafor | Prompt Engineer |
| Dr. Yewande Diallo | AI Ethicist |
| Dr. Amara Osei-Bonsu | AI Research Scientist |
| Dr. Aisha Mensah | Cognitive Scientist |
| Dr. Kenji Watanabe | DevOps / Git Workflow Engineer |

## PM Assessment
Two infrastructure gaps: (1) after `push-merge`, the calling worktree branch is left at pre-fix code even though main was updated; (2) installations that partially migrated from `atelier.db` to `memex.db` have no detection tool.

## Agenda
1. How should `push-merge` propagate the merged code back to the calling worktree branch?
2. What is the right detection and warning strategy for coexisting `atelier.db` / `memex.db`?

## Discussion

### Agenda Item 1: Worktree branch sync after push-merge

**Proposals:**
- Dr. Kenji Watanabe (DevOps): `--ff-only` first, fall back to `--no-ff` merge if fast-forward fails.
- Dr. Fatima Al-Rashid (Safety): Reject `--no-ff` fallback — auto-creating a merge commit in the caller's branch crosses an ownership boundary. Gate on clean working tree. `--ff-only` only; on failure, warn with manual instructions and do not exit non-zero.
- Dr. Nadia Petrov (Systems Architect): Agrees with Al-Rashid. The cycle itself succeeded; worktree sync failure is advisory, not fatal.

**Discussion:** Watanabe's `--no-ff` fallback was dropped. The common case (worktree with no local divergence) is served by `--ff-only`. When the worktree has local commits, a manual `git merge main` is the appropriate human action — the agent should not make that decision automatically. Gating on clean working tree prevents interfering with in-progress work.

**Decision:** `--ff-only`-only sync with graceful warning on failure; gate on clean worktree — *Unanimous*

### Agenda Item 2: `atelier.db` / `memex.db` coexistence detection

**Proposals:**
- Dr. Nadia Petrov (Systems Architect): Approach B — standalone `scripts/migrate_db_rename.py`. `get_connection()` should not contain filesystem heuristics.
- Dr. Fatima Al-Rashid (Safety): Concurs. Detection script is safe to auto-merge (read-only detection). Manual copy/delete step kept human-initiated.
- Dr. Yusuf Okafor (Prompt Engineer): Script should exit code 1 when ambiguity detected, 0 when clean, to enable scripted checks.

**Discussion:** Coupling filesystem glob logic to `get_connection()` was unanimously rejected — wrong layer. Standalone script with clear report, explicit manual step, exit codes is the right approach. Script should never auto-delete or auto-copy.

**Decision:** Create `scripts/migrate_db_rename.py`; no changes to `db.py` — *Unanimous*

## Decisions Log
1. `push-merge` handler: add `--ff-only` worktree sync with clean-tree gate and graceful fallback warning — `scripts/self_improve.py`
2. Create `scripts/migrate_db_rename.py` for `atelier.db`/`memex.db` coexistence detection — `scripts/migrate_db_rename.py`

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | Worktree `--ff-only` sync block in `push-merge` handler | `scripts/self_improve.py` | Dr. Kenji Watanabe |
| 2 | `migrate_db_rename.py` detection script | `scripts/migrate_db_rename.py` | Dr. Nadia Petrov |

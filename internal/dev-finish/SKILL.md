---
description: Use when QA is approved and the work is ready to hand off — commits the work to Atelier's feature branch (never pushes or opens a PR — that is the human's step), or abandons it, with pre-flight checks and phase completion.
---

# dev:finish

Final handoff step. Runs after `qa:approved`. Verifies the work is committed on Atelier's feature branch, hands the unpushed branch off to the human (who owns push + PR + merge — A6), and advances the project to `handoff:complete`. Atelier MUST NOT `git push`, open a PR, or merge into the base/production branch at any point in this procedure.

> **Prerequisites**
> - Mode: Memex or Local (mode-symmetric — `workflow.py` + `session.py` dispatch via `backend.py`)
> - Required: `qa:approved` phase reached; working tree clean; CI green
> - Required tables: `projects`, `skill_gates`, `phase_bypasses`, `sessions` — seeded by Atelier bootstrap

## Hard gate

Requires `qa:approved`.

## Procedure

1. **Check the phase gate:**
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:finish
   ```
   Parse JSON output. If `allowed` is `false`, apply standard bypass-confirm-log flow before continuing.

2. **Pre-flight** (all blocking — do not skip):

   | Check | Command | Required result |
   |---|---|---|
   | Tests green | `pytest -q --tb=short` | 0 failures — show full summary line |
   | Working tree clean | `git status --short` | No output |
   | No open assigned tasks | `python3 atelier/scripts/tasks.py list --project_id <project_id> --status assigned` | Empty list |
   | All work committed | `git status --short` | No output (already covered above — the feature branch must hold every change) |

   If any check fails: stop. State what failed and what must be resolved. Do not advance phase.

   > **CI is the human's post-handoff gate, not Atelier's pre-flight.** Atelier
   > never pushes (A6), so there is no remote CI run for the feature branch at
   > finish time. Atelier's blocking gate is the LOCAL test suite (`pytest`)
   > plus a clean tree. Remote CI (`gh run list`) is verified by the human
   > AFTER they push the branch — surface it in the handoff note, do not run a
   > remote CI check here as if Atelier had pushed.

3. **Advance phase:**
   ```
   python3 atelier/scripts/workflow.py <db_path> advance <project_id> handoff:open
   ```

4. **Hand off the feature branch — Atelier does NOT push, open a PR, or merge (A6).**

   Atelier's responsibility ends when all work is committed on its own feature
   branch. **Push, PR creation, and merge into the base/production branch
   belong exclusively to the human.** Atelier MUST NOT run `git push`,
   `gh pr create`, or any merge into the base branch. Ask the user to choose:

   > **How do you want to close out this work?**
   > - **(a) Hand off for integration (default)** — Atelier confirms every change is committed on `<branch>` and leaves it UNPUSHED. Atelier prints the branch name and the exact push + PR commands for *you* to run. Atelier does not run them.
   > - **(c) Abandon** — discard Atelier's own feature branch. Requires your explicit confirmation. Write a session note explaining why.

   Wait for the user's explicit choice before proceeding.

   > **(agent-team mode only) — Per-run branch consolidation + retrospective**
   >
   > This sub-procedure applies ONLY when this cycle ran in **agent-team
   > mode** (a real per-run feature branch + task worktrees were created). It
   > REPLACES the single-tree `(a)` option below for team mode. Sub-agent /
   > Memex finishes create no team worktrees, so SKIP this block and use
   > `(a)`/`(c)` as written.
   >
   > A team-mode run fans N task worktrees off a single per-run feature branch
   > `atelier/<slug>`; finish merges them all into that one branch (Atelier's
   > OWN branch — allowed) and then HANDS THAT BRANCH OFF to the human. Atelier
   > MUST NOT push it or open a PR. All steps go through `scripts/finish_pr.py`
   > (thin, tested git layer — it does NOT call `worktree.merge_back`, which
   > would merge into the base/production branch).
   >
   > 1. **Resolve the canonical feature branch (never re-derive the slug — F4).**
   >    ```
   >    # Run as: python3 -c "<contents below>" (replace <repo_root>, <base>)
   >    from pathlib import Path
   >    from scripts import finish_pr
   >    from scripts.scope import resolve_scope
   >
   >    scope = resolve_scope()
   >    slug = scope.project["slug"]          # canonical per-run slug
   >    branch = finish_pr.resolve_or_create_feature_branch(
   >        Path("<repo_root>"), slug, "<base>")
   >    print(branch)                          # canonical atelier/<slug>, read back
   >    ```
   >    `branch` (the string returned / read back) is canonical — pass it
   >    verbatim into the merge step and into the human handoff (the branch the
   >    human will push + open a PR from); never retype the slug.
   >
   > 2. **Merge the wave worktrees in dependency order.**
   >    ```
   >    result = finish_pr.merge_worktrees(
   >        Path("<repo_root>"), branch, "<base>",
   >        task_branches_in_dep_order)   # wave/DAG order; sibling-namespace
   >                                      # branches (NOT nested under atelier/<slug>)
   >    ```
   >    Clean worktrees auto-remove; worktrees still carrying uncommitted
   >    PROJECT changes are PRESERVED and listed in `result.dirty_preserved`
   >    (`.claude/`-only dirt counts CLEAN). A conflicting task aborts cleanly
   >    (`git merge --abort`) and lands in `result.conflicts` — surface those
   >    in the PR body rather than force-merging.
   >
   >    **Task-branch naming (sibling, NEVER nested) — use the canonical helper.**
   >    The FUTURE worktree-creation dispatch layer MUST mint each task branch via
   >    `finish_pr.task_branch_name(slug, task_id)` →
   >    `atelier/<slug>-task-<task_id>`, a SIBLING of the feature branch. NEVER
   >    re-derive the name ad hoc and NEVER nest it under the feature branch
   >    (`atelier/<slug>/<task_id>`): git's loose-ref storage cannot hold both a
   >    ref FILE at `refs/heads/atelier/<slug>` and a ref DIRECTORY beneath it, so
   >    the nested form is a hard D/F conflict. `merge_worktrees` validates every
   >    branch it is handed against this sibling contract
   >    (`finish_pr._BRANCH_RE`) and rejects any non-conforming / leading-dash
   >    name (also a git option-injection guard), so a dispatch layer that skips
   >    `task_branch_name` will fail loud at the merge boundary.
   >    ```
   >    # When creating each task worktree (future dispatch layer):
   >    branch_for_task = finish_pr.task_branch_name(slug, task_id)
   >    # git worktree add -b <branch_for_task> <wt_dir> <base>
   >    ```
   >
   > 3. **Hand the consolidated branch off to the human — do NOT open a PR.**
   >    All task work now lives on `branch` (Atelier's feature branch). Atelier
   >    MUST NOT call `finish_pr.open_pr` (it pushes + runs `gh pr create`),
   >    MUST NOT `git push`, and MUST NOT merge into the base branch. Print the
   >    branch name, the per-task summary, any `result.conflicts` /
   >    `result.dirty_preserved`, and the exact commands FOR THE HUMAN TO RUN
   >    (see the **Human handoff** block under option (a) below). The human owns
   >    push + PR + merge.
   >
   > 4. **Write the retrospective (BEFORE the session record + Retro).**
   >    ```
   >    finish_pr.write_retrospective(
   >        workspace_id=scope.workspace["id"] if scope.workspace else None,
   >        project_id=scope.project["id"] if scope.project else None,
   >        title="Finish: <project name>",
   >        body="<what was consolidated onto the feature branch; per-task "
   >             "summary; conflicts/dirty_preserved>",
   >        pr_url=None)   # no PR — Atelier does not open one (A6)
   >    ```
   >    One `backend.write_document(domain='project_doc',
   >    subdomain='finish-result', metadata={phase:'finish', pr_url})` via the
   >    A2 facade — mode-symmetric, NO mode branch. `pr_url` is `None` because
   >    the human opens the PR later. Fire it BEFORE the session record so the
   >    durable artifact survives even if a later step slips.
   >
   > Then continue to step 5 (session record — `--notes "handoff: <branch>"`)
   > and step 6 (Retro). Do NOT also run `(a)` below. Host workers are reaped by
   > the engine — there is no harness team to tear down.

   **Option (a) — Hand off for integration (Atelier does NOT push or PR):**
   Confirm the tree is clean and every change is committed on `<branch>`
   (`git status --short` empty; `git log <base>..<branch> --oneline` shows the
   work). Atelier then STOPS at the branch and prints the handoff for the human.

   > **Human handoff — commands for YOU to run. Atelier MUST NOT execute these.**
   > ```
   > git push origin <branch>
   > gh pr create --title "<project name>" --body "<summary from design doc>"
   > ```
   > Atelier never runs `git push`, `gh pr create`, or a merge into `<base>` —
   > push, PR, and merge are yours (A6). The committed feature branch is the
   > handoff artefact; the phase advances once the work is committed on it.

   **Option (c) — Abandon:**
   Ask: "Confirm abandoning branch `<branch>`? This deletes the branch. (yes/no)" Wait for yes, then:
   ```
   git branch -D <branch>
   ```
   Write a session note with the reason via `--notes "<reason>"` in step 5.

5. **Write session record and advance to complete:**
   ```
   python3 atelier/scripts/session.py write <project_id> <agent_id> handoff:open complete \
     --accomplished "<what was integrated>" \
     --next-action "Human to push <branch> + open PR — start a new project for follow-on work" \
     [--notes "handoff: <branch> (unpushed — human owns push/PR/merge) | or abandon reason"]
   ```
   Then advance:
   ```
   python3 atelier/scripts/workflow.py <db_path> advance <project_id> handoff:complete
   ```

6. **Retro — surface phase bypasses:**

   ```python
   # Run as: python3 -c "<contents below>" (replace <project_id>)
   from collections import Counter
   from scripts import backend

   rows = backend.list_phase_bypasses(project_id=<project_id>)
   if not rows:
       print("No phase bypasses during this project's lifecycle.")
   else:
       counts = Counter((r["from_phase"], r["to_phase"]) for r in rows)
       for (from_phase, to_phase), n in counts.most_common():
           print(f"from {from_phase} → {to_phase}: {n} bypass(es)")
   ```

   If any bypasses exist, present the summary. Ask the user if any should be captured as lessons.

## Hard rules

- **Atelier NEVER pushes, opens a PR, or merges into the base/production branch (A6).** `git push`, `gh pr create`, and merge-to-base are the human's steps. `finish_pr.open_pr` and `worktree.merge_back` MUST NOT be called from this procedure.
- Atelier commits ONLY to its own feature branch — never to `main`/the production branch.
- The handoff outcome ends with the work committed on the UNPUSHED feature branch; the human owns push + PR + merge.
- Never execute abandon (branch deletion) without explicit user confirmation.
- Pre-flight is not skippable — local tests must be green and the tree clean before handoff.
- Remote CI is the human's post-push gate, not asserted here from memory.
- `handoff:complete` is terminal — new work requires a new project.
- Both outcomes (handoff and abandon) write a session record.

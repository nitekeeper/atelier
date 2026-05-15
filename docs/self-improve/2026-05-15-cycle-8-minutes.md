# Self-Improvement Meeting — Cycle 8
**Date:** 2026-05-15 UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. Nadia Petrov | Agent Systems Architect |
| Dr. Fatima Al-Rashid | AI Safety Researcher |
| Dr. Yusuf Okafor | Prompt Engineer |
| Dr. Aisha Mensah | Cognitive Scientist |
| Dr. Yewande Diallo | AI Ethicist |
| Dr. Amara Osei-Bonsu | AI Research Scientist |
| Dr. Kenji Watanabe | DevOps / Git Workflow Engineer |

## PM Assessment
Targeted fix cycle for three session-boundary robustness gaps identified in the open task backlog: (1) `merge_back()` false-exits on untracked `.claude/` dirs, (2) `save` Phase B has no path when no projects exist, (3) `ingest` crashes opaquely when Memex CLI is absent.

## Agenda
1. How should `merge_back()` detect and tolerate `.claude/`-only untracked files without weakening its dirty-workspace guard for real changes?
2. What is the minimal, agent-proof Phase B early-exit in `save` when no project record exists?
3. What is the right fallback routing for `ingest` when the Memex CLI is absent?

## Discussion

### Agenda Item 1: `merge_back()` `.claude/` untracked filtering

**Proposals:**
- Dr. Kenji Watanabe (DevOps): Separate `--porcelain` lines into three buckets: `dirty_lines` (non-`??`), `untracked_claude` (`?? .claude/…`), `untracked_other`. Exit on dirty or untracked_other; warn and continue on untracked_claude only.
- Dr. Nadia Petrov (Systems Architect): Surgical filter approach. Preserves the guard for real changes. Non-destructive.
- Dr. Fatima Al-Rashid (Safety): Raised Windows path-separator concern. Resolved — `git status --porcelain` always uses `/` regardless of OS. Also flagged the silent-bypass risk; addressed by the explicit warning message.
- Dr. Aisha Mensah (Cognitive): Supplied warning message: "Note: Untracked .claude/ files detected and skipped. Add '.claude/' to .git/info/exclude to silence."

**Discussion:** Al-Rashid's Windows concern was resolved by confirming `git status --porcelain` always produces forward-slash paths. The three-bucket approach preserves the guard for all non-`.claude/` untracked files. Warning message converts silent bypass into actionable guidance.

**Decision:** Apply Watanabe's three-bucket filter with Mensah's warning message — *Unanimous*

### Agenda Item 2: `save` Phase B empty project list

**Proposals:**
- Dr. Yusuf Okafor (Prompt Engineer): Exact Step 4 replacement text with explicit `[]` early-exit branch — hard stop with recovery instruction.
- Dr. Fatima Al-Rashid (Safety): Hard stop required. Writing a session row with no project is a logic error, not a warning situation. Must not continue.
- Dr. Nadia Petrov (Systems Architect): Aligns with existing hard rule pattern — extends "abort on non-zero" to data-empty case.
- Dr. Aisha Mensah (Cognitive): Message: "No projects found in Memex. Skipping Phase B. Create a project first: `projects.py create`"

**Discussion:** Unanimous that this must be a hard stop, not a soft warning. Okafor's replacement text adopted with Mensah's user message integrated.

**Decision:** Apply Okafor's Step 4 replacement with hard stop on empty list — *Unanimous*

### Agenda Item 3: `ingest` CLI fallback routing

**Proposals:**
- Dr. Yusuf Okafor (Prompt Engineer): New Step 1b — `memex --version` probe, exit 0 proceed, non-zero halt with install instructions. Hard stop.
- Dr. Fatima Al-Rashid (Safety): Auto-routing to auto-memory is REJECTED. Creates silent knowledge divergence — future Memex-aware agents won't see routed content. "Never silently redirect where content goes."
- Dr. Nadia Petrov (Systems Architect): Agrees with loud halt. Converts opaque shell error into diagnosable, recoverable state.
- Dr. Aisha Mensah (Cognitive): Loud failure message variant (a) adopted: "ERROR: Memex CLI not found. Install: pip install memex. Re-run `ingest` once Memex is available."

**Discussion:** Al-Rashid's safety argument was decisive — auto-memory fallback creates invisible knowledge loss that violates the spirit of the ingest hard rule "never invent or summarize content" by silently changing where content is stored. Loud failure is the correct policy. Auto-memory fallback DROPPED.

**Decision:** Apply Step 1b with loud failure only; auto-memory fallback dropped — *Unanimous*

## Decisions Log
1. `scripts/worktree.py` `merge_back()`: filter `.claude/`-only untracked entries before dirty-workspace exit — `scripts/worktree.py`
2. `skills/save/SKILL.md` Step 4: add hard-stop branch when `projects.py list` returns empty — `skills/save/SKILL.md`
3. `skills/ingest/SKILL.md`: add Step 1b — `memex --version` probe with hard stop on failure — `skills/ingest/SKILL.md`

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | Three-bucket `.claude/` filter in `merge_back()` | `scripts/worktree.py` | Dr. Kenji Watanabe |
| 2 | Step 4 empty-list hard-stop in Phase B | `skills/save/SKILL.md` | Dr. Yusuf Okafor |
| 3 | Step 1b `memex --version` probe | `skills/ingest/SKILL.md` | Dr. Yusuf Okafor |

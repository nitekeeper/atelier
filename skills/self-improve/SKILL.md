# dev:self-improve

Autonomous multi-agent improvement of Atelier's own code, skills, and structure. Uses an isolated git clone, a structured meeting with world-class expert agents, unanimous consensus, and a full test gate before merging.

## Hard gate

**User-initiated only.** No agent may call this skill from within any workflow or script. If you are an agent, do not invoke `dev:self-improve`.

## Invocation

```
dev:self-improve [--cycles N] [--subject "<area to improve>"]
```

- `--cycles N` — number of independent improvement cycles (default: 1). Each cycle is fully independent.
- `--subject` — optional focus area (e.g., `"improve QA skill procedure"`). If omitted, PM decides.

Cycles run sequentially. A failure in one cycle does not block the next.

## Procedure

### Phase 1 — Agenda setting (PM)

1. Record cycle start time (UTC).

2. Read the entire repository:
   - All `skills/*/SKILL.md` files
   - All `scripts/*.py` files
   - All `migrations/*.sql` files
   - All `tests/` files
   - `docs/`, `CHANGELOG.md`, `CLAUDE.md`

3. **If `--subject` is provided:** Focus analysis on that subject area.
   **If no subject:** Audit the full codebase and decide which area most needs improvement. Record your reasoning — it becomes the PM Assessment section.

4. Produce:
   - A numbered agenda (each item is a specific improvement question)
   - A list of agents to summon from the 61-role roster (select by domain relevance)

5. Draft the minutes file header in working context. Use this format (it will be written to the clone at step 11):

```markdown
# Self-Improvement Meeting — Cycle N
**Date:** YYYY-MM-DD HH:MM UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. [Name] | [Role] |

## PM Assessment *(only if no --subject provided)*
[Reasoning for chosen focus area]

## Agenda
1. [Improvement question]
2. ...
```

**Agents with standing relevance to every cycle:**
- Agent Systems Architect (Dr. Nadia Petrov) — agent orchestration and coordination
- AI Safety Researcher (Dr. Fatima Al-Rashid) — failure modes and alignment
- Prompt Engineer (Dr. Yusuf Okafor) — SKILL.md procedure quality
- AI Ethicist (Dr. Yewande Diallo) — bias and governance
- AI Research Scientist (Dr. Amara Osei-Bonsu) — theoretical soundness
- Cognitive Scientist (Dr. Aisha Mensah) — cognitive alignment of procedures

### Phase 2 — Parallel pre-analysis

6. Dispatch all summoned agents in parallel. Each independently reads the codebase areas relevant to their domain and writes a structured proposal:
   - What they found (specific files, patterns, problems)
   - What they propose to change and why
   - Risk classification: destructive or non-destructive
   - Any dependencies or conflicts with other agents' domains they anticipate

7. Collect all proposals before the meeting begins.

### Phase 3 — Synthesis meeting

8. PM facilitates a structured debate of each agenda item. For each item:
   - Present all proposals
   - Agents raise objections or support
   - Revise until unanimous agreement, or drop the item
   - Record the outcome in the minutes

9. Complete the minutes document:

```markdown
## Discussion

### Agenda Item 1: [Title]
**Proposals:**
- Dr. [Name] ([Role]): [Proposal summary]

**Discussion:** [Debate and resolution summary]

**Decision:** [Agreed change] — *Unanimous*
*or*
**Decision:** DROPPED — [reason no consensus reached]

## Decisions Log
1. [Decision text] — [file(s) affected]
2. ...

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | [what] | [where] | [agent] |
```

### Phase 4 — Implementation in experiment clone

> The clone is created at `<production_repo_parent>/experiment/<repo_name>/`. This is the path printed as `CLONE_DIR`. The production repo is never modified during the cycle.

10. Set up the isolated clone and feature branch:
```
python scripts/self_improve.py clone <cycle_n>
```
The command prints `CLONE_DIR=<path>` and `BRANCH=<name>`. Record both.

11. Write the completed minutes file into the clone at:
```
<clone_dir>/docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md
```

12. Each assigned agent implements their action items by making changes directly in `<clone_dir>`.

13. Check for destructive changes:
```
python scripts/self_improve.py check-destructive <clone_dir>
```
- Exit 0: no destructive changes — proceed.
- Exit 1: review the JSON output. For each destructive change, ask the user:
  > "Cycle N proposes a destructive change: [description]. Approve? (y/n)"
  - Approved: proceed.
  - Rejected: revert that change in the clone, re-run check until exit 0.
- If all proposed changes were rejected and no changes remain, ABORT the cycle. Append to minutes: `## Outcome\nABORTED — all proposed changes rejected`. Proceed directly to step 17.

### Phase 5 — Quality gates, push, cleanup

14. Run the full test suite in the clone:
```
python scripts/self_improve.py run-tests <clone_dir>
```
- **Pass (exit 0):** proceed to step 15.
- **Fail (exit 1):** ABORT. Do NOT execute step 15 or 16. The branch is NOT pushed. Append to minutes: `## Outcome\nFAILED — tests did not pass`. Proceed directly to step 17.

15. Commit all changes:
```
python scripts/self_improve.py commit <clone_dir> <cycle_n> "<subject>" "<d1>|<d2>" "<p1>|<p2>" <test_count> "docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md"
```

Where:
- `<subject>` — user-provided subject or the string `PM-directed`
- `<d1>|<d2>` — pipe-separated decisions from the log
- `<p1>|<p2>` — pipe-separated participant names
- `<test_count>` — number from `run-tests` output

> **Note:** Decision and participant strings must not contain `|` characters. If a decision text contains `|`, replace it with ` / ` before passing.

16. Push and merge:
```
# If all changes are non-destructive (or all approved):
python scripts/self_improve.py push-merge <clone_dir> <branch>

# If any destructive change is awaiting approval (skip auto-merge):
python scripts/self_improve.py push-merge <clone_dir> <branch> skip
```

17. Clean up:
```
python scripts/self_improve.py cleanup
```

18. The `push-merge` command automatically pulls main after a successful auto-merge. No additional pull step is needed.

19. Print cycle summary:
```
Cycle N — [PASSED / FAILED / AWAITING APPROVAL]
Subject: [subject or PM-directed]
Participants: [N agents]
Decisions: [N agreed / M dropped]
Minutes: docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md
Branch: self-improve/cycle-N-YYYY-MM-DD [merged / pending / not pushed]
```

## Hard rules
- User-initiated only. Abort if called from an agent context.
- Unanimous consent required. No item proceeds with a dissenting agent.
- Tests must pass before commit. Failure aborts the cycle — no exceptions.
- Destructive changes require explicit user approval before merging.
- `experiment/` is always deleted, whether the cycle passes, fails, or aborts.
- Every cycle produces a complete Markdown meeting minutes document.
- One commit per cycle — changes and minutes file together.

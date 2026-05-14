# Atelier Dev-Skill Integration Design

> **Status:** Approved — all 5 sections reviewed and confirmed.
> **Date:** 2026-05-13
> **Context:** Dev-skill was built as a standalone product but belongs inside Atelier as its core development workflow layer. This spec covers the full integration: DB model changes, unified phase vocabulary, session continuity, multi-agent execution, role roster, and cleanup.

---

## Goals

- Integrate dev-skill content (7 skill files, session hook, WORK.md schema) into Atelier's database-backed model
- Replace WORK.md flat-file state tracking with a queryable `sessions` table
- Move the phase state machine from hardcoded Python dicts to `phases` + `phase_transitions` DB tables
- Establish a unified 19-phase vocabulary across all dev workflow skills
- Support multi-agent parallel execution via `parallel_group` on tasks
- Seed 46 world-class expert role profiles into the `roles` table
- Clean up the dev-skill repo (wrong architectural turn)

## Non-Goals

- File system / wiki management (deferred to next design session)
- Memex integration changes
- New skill development beyond the dev workflow set
- Cross-repo concerns (this is Atelier-only)
- Consumer-facing release (no `dist/` cut in this plan)

---

## Architecture Overview

```
Atelier
├── Memory layer      → Memex (wiki, lessons, ask)
└── Dev workflow layer → dev:* skills (design, plan, tdd, review, security, qa, diagnose, handoff)
                         └── Backed by DB (sessions, phases, phase_transitions, tasks, roles, agents)
```

Dev-skill was a wrong turn — it attempted to be a standalone product with WORK.md as state. The correct model: dev workflow is an Atelier feature, state lives in the DB, skills are the interface.

**PM is the coordination hub.** The Product Manager agent bridges the user and sub-agents. PM reads the latest session at open, prioritizes open tasks, dispatches agents in parallel, and writes session notes at close.

---

## Section 1 — Session Continuity

### Problem

The existing `session.py` reads and writes `.ai/work.md` as flat text. This is not queryable, not prunable, and breaks the DB-first architecture. PM has no working memory between sessions.

### Decision

Add a `sessions` table as PM working memory. One row per session close. Prunable — PM does not need full history since git retains it. DB stays lean.

### Schema

```sql
CREATE TABLE sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          TEXT REFERENCES projects(id),
    agent_id            TEXT REFERENCES agents(id),
    phase               TEXT,
    pre_diagnose_phase  TEXT,   -- saved when entering diagnose:open; restored on diagnose:resolved
    current_tasks       TEXT,
    accomplished        TEXT,
    next_action         TEXT,
    status              TEXT CHECK(status IN ('in-progress', 'blocked', 'complete')),
    blocking_reason     TEXT,
    pm_notes            TEXT,
    opened_at           TIMESTAMP,
    closed_at           TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### session.py CLI (rewrite)

```
session.py write      <project_id> <agent_id> <phase> <status> [--notes ...] [--accomplished ...] [--next ...]
session.py read-latest <project_id>
session.py list       <project_id> [--limit N]
session.py update     <session_id> <field> <value>
session.py prune      <project_id> --keep <n>
```

### Lifecycle

- **Open:** Hook reads `session.py read-latest <project_id>` → announces phase + pm_notes to Claude context
- **Close:** PM writes `session.py write ...` before ending session
- **Prune:** PM prunes old sessions when they are no longer useful; git retains full history

---

## Section 2 — Phase State Machine

### Problem

`workflow.py` contains hardcoded `VALID_TRANSITIONS` and `PHASE_GATES` dicts. Adding or renaming a phase requires code changes. Phase names were inconsistent across dev-skill and existing Atelier skills.

### Decision

Move the state machine to `phases` + `phase_transitions` DB tables. Workflow becomes data-driven and extensible without code changes.

### Schema

```sql
CREATE TABLE phases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT UNIQUE NOT NULL,       -- e.g. "tdd:green"
    skill        TEXT NOT NULL,              -- e.g. "dev:tdd"
    state        TEXT NOT NULL,              -- e.g. "green"
    description  TEXT NOT NULL,
    is_terminal  BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE phase_transitions (
    from_phase   TEXT REFERENCES phases(name),
    to_phase     TEXT REFERENCES phases(name),
    PRIMARY KEY (from_phase, to_phase)
);
```

### Unified Phase Vocabulary (19 phases)

| Phase | Skill | State | Terminal? | Description |
|---|---|---|---|---|
| `design:open` | `dev:design` | open | No | Grilling and drafting in progress |
| `design:approved` | `dev:design` | approved | No | Design document approved by user |
| `plan:open` | `dev:plan` | open | No | Implementation plan being written |
| `plan:approved` | `dev:plan` | approved | No | Plan approved, ready for TDD |
| `tdd:red` | `dev:tdd` | red | No | Failing tests written |
| `tdd:green` | `dev:tdd` | green | No | Tests passing with minimal implementation |
| `tdd:clean` | `dev:tdd` | clean | No | Code refactored, tests still passing |
| `review:open` | `dev:review` | open | No | Code review in progress |
| `review:changes-requested` | `dev:review` | changes-requested | No | Reviewer requested changes |
| `review:approved` | `dev:review` | approved | No | Code review passed |
| `security:open` | `dev:security` | open | No | Security review in progress |
| `security:changes-requested` | `dev:security` | changes-requested | No | Security issues raised |
| `security:approved` | `dev:security` | approved | No | Security review passed |
| `qa:open` | `dev:qa` | open | No | QA review in progress |
| `qa:changes-requested` | `dev:qa` | changes-requested | No | QA found blocking issues |
| `qa:approved` | `dev:qa` | approved | No | QA passed, ready for handoff |
| `diagnose:open` | `dev:diagnose` | open | No | Bug diagnosis in progress |
| `diagnose:resolved` | `dev:diagnose` | resolved | No | Bug diagnosed and fixed |
| `handoff:complete` | `dev:handoff` | complete | Yes | Session closed, WORK snapshot written |

### Valid Transitions (key paths)

```
design:open → design:approved
design:approved → plan:open
plan:open → plan:approved
plan:approved → tdd:red
tdd:red → tdd:green
tdd:green → tdd:clean
tdd:clean → tdd:red          (next cycle)
tdd:clean → review:open
review:open → review:changes-requested
review:changes-requested → review:open
review:open → review:approved
review:approved → security:open
security:open → security:changes-requested
security:changes-requested → security:open
security:open → security:approved
security:approved → qa:open
qa:open → qa:changes-requested
qa:changes-requested → qa:open
qa:open → qa:approved
qa:approved → handoff:complete

# Diagnose can be entered from any phase
* → diagnose:open
diagnose:resolved → <pre_diagnose_phase>

# pre_diagnose_phase: stored in sessions table when diagnose:open is written.
# On diagnose:resolved, workflow.py reads the most recent session's pre_diagnose_phase
# and restores the project to that phase.
```

### workflow.py refactor

Replace hardcoded dicts with DB queries:

```python
def get_valid_transitions(from_phase: str) -> list[str]:
    # SELECT to_phase FROM phase_transitions WHERE from_phase = ?

def check_gate(project_id: str, skill: str) -> bool:
    # SELECT phase FROM projects WHERE id = ?
    # Validate against phases table
```

---

## Section 3 — Multi-Agent Parallel Execution

### Problem

Tasks have a single `priority INTEGER` field. No support for grouping tasks that should run in parallel. Only one agent per role is assumed.

### Decision

- Add `parallel_group TEXT` to `tasks` — tasks sharing a group run concurrently
- Change `priority` from INTEGER to TEXT with enum values: `critical`, `high`, `medium`, `low`
- Allow multiple agent rows per `role_id` in the `agents` table (already supported by schema; no change needed)
- PM dispatches all tasks in a parallel group simultaneously

### Tasks table changes

```sql
-- Add parallel_group
ALTER TABLE tasks ADD COLUMN parallel_group TEXT;

-- Migrate priority INTEGER → TEXT
-- (handled in migration 004 with data transformation)
```

### Priority values

| Value | Meaning |
|---|---|
| `critical` | Blocking — must resolve before anything else |
| `high` | Should be in current sprint |
| `medium` | Planned but not urgent |
| `low` | Nice to have |

### PM parallel dispatch pattern

```
PM reads open tasks for project
  → groups by parallel_group
  → dispatches all tasks in same group simultaneously
  → monitors completion
  → writes session notes at close
```

PM also asks user to confirm current priority at session open — priorities can change overnight.

---

## Section 4 — Session Hook

### Problem

`hooks/session_open.py` reads `.ai/work.md` as flat text. Must be adapted to read from DB via `session.py read-latest`.

### Behaviour by state

| State | Hook behaviour |
|---|---|
| Session found | Announce: project, phase, pm_notes, next_action |
| No session for project | Announce: project exists, no prior session recorded |
| DB missing | **Option B:** Emit warning, continue without session context. Do not block Claude. |
| Table missing (DB exists but sessions table absent) | Emit warning (migrations may be pending), continue |
| DB corrupted | Emit warning, continue. Do not crash the session. |

**Option B rationale:** A missing or corrupted DB should never block a work session. The hook is informational. Claude continues with reduced context rather than hard failure.

---

## Section 5 — Cleanup & Migration

### 5a. Dev-skill repo cleanup

| Action | Target |
|---|---|
| Delete GitHub repo | `nitekeeper/dev-skill` |
| Archive local repo | `C:\Users\user\Documents\Skills\dev-skill\` |
| Update ROADMAP.md | Reframe "Product 2 — Dev Skill" as Atelier dev workflow integration |
| Update products-registry | Remove dev-skill row if present |

### 5b. Database migrations

| File | Purpose |
|---|---|
| `migrations/002_sessions.sql` | Add `sessions` table |
| `migrations/003_phases.sql` | Add `phases` + `phase_transitions` tables; seed 19 phases and transitions |
| `migrations/004_tasks_parallel.sql` | Add `parallel_group TEXT`; migrate `priority` INTEGER → TEXT |
| `migrations/005_seed_roles.sql` | Seed all 47 expert role profiles into `roles` table |

### 5c. Content migration from dev-skill → Atelier

| Source | Destination | Note |
|---|---|---|
| 7 `skills/dev-*/SKILL.md` files | Update Atelier `skills/dev-*/SKILL.md` — replace WORK.md calls with DB calls via `workflow.py` and `session.py` | Migrated |
| `hooks/session_open.py` | Adapt Atelier hook — replace flat-file read with `session.py read-latest` | Migrated |
| `docs/WORK_MD_SCHEMA.md` | Retired — superseded by `phases` table. Archive only. | Retired |
| `docs/HOOKS_SETUP.md` | Merge into Atelier setup docs | Migrated |
| `README.md` relevant content | Merge into Atelier USER_GUIDE | Migrated |
| — | `skills/dev-security/SKILL.md` | **New** — `dev:security` skill does not exist in dev-skill. Must be created from scratch as part of this integration. Covers security review phase with `security:open → security:changes-requested ↔ security:approved` loop. |

---

## Section 6 — Role Roster

### Design principles

- **Discipline-based, not technology-locked.** A Backend Engineer covers PHP, Java, Go, Node.js, Python — the discipline stays the same across tech stacks.
- **World-class experts.** All profiles are PhD-level or equivalent, with 20+ years of deep domain experience. Recognized authorities, not generalists.
- **Version-agnostic.** No pinned version numbers in any profile. Use "latest specification", "latest standard", "current stable release." Profiles must not go stale as specifications evolve.
- **Multiple agents per role.** One PM, one Architect, one SDET — but many Software Engineers, Frontend Engineers, etc. The `agents` table allows N rows per `role_id`.

### Profile template

```
**<Job Title>**
*Dr./Prof. <Name> — PhD in <field>, <University>. <N> years of experience. <Notable achievements>.*

**Expertise:** <version-agnostic technology list>
**Responsibilities:** <what they own>
**Works with:** <inter-role relationships>
**Does not:** <explicit boundaries>
**Communication style:** <how they engage>
```

### Role roster (46 roles)

Full profiles are in `migrations/005_seed_roles.sql`. Titles:

**Coordination**
1. Product Manager

**Architecture**
2. Software Architect
3. Systems Architect

**Engineering — Backend**
4. Software Engineer (Backend)

**Engineering — Frontend**
5. Frontend Engineer

**Engineering — Full-Stack & Mobile**
6. Full-Stack Engineer
7. Mobile Engineer (iOS)
8. Mobile Engineer (Android)
9. Mobile Engineer (Cross-platform)

**Engineering — Data & ML**
10. Data Engineer
11. Machine Learning Engineer
12. Data Scientist

**Engineering — Infrastructure**
13. DevOps / Platform Engineer
14. Site Reliability Engineer (SRE)
15. Cloud Infrastructure Engineer

**Engineering — Database**
16. Database Engineer

**Engineering — Security**
17. Security Engineer
18. Application Security Engineer

**Engineering — Quality**
19. SDET (Software Development Engineer in Test)
20. QA Engineer
21. Performance Engineer

**Engineering — Embedded & Systems**
22. Embedded Systems Engineer
23. Firmware Engineer

**Engineering — Specializations**
24. API Engineer
25. Integration Engineer
26. Search Engineer
27. Real-Time Systems Engineer
28. Blockchain Engineer
29. Game Engineer
30. Graphics Engineer
31. Compiler Engineer
32. Developer Tools Engineer
33. WebAssembly Engineer
34. CLI Engineer

**Design & UX**
35. UX / UI Designer
36. Accessibility Specialist
37. Design Systems Engineer

**Data & Analytics**
38. Business Intelligence Engineer
39. Analytics Engineer

**Operations & Delivery**
40. Technical Program Manager
41. Release Manager
42. Documentation Engineer / Technical Writer

**Specialized**
43. SEO Engineer
44. Developer Advocate
45. Localization / Internationalization Engineer
46. Systems Engineer *(Rust / C / C++ / WebAssembly — systems-level programming)*

> **Note:** All profiles are seeded in `migrations/005_seed_roles.sql` with version-agnostic language throughout. Profiles follow the standard template above.

---

## Assumptions & Dependencies

- Atelier DB is SQLite, migrations runner is `scripts/migrate.py`
- Existing migrations 001 through current are applied before 002–005
- `tasks.priority` column exists as INTEGER (confirmed from `001_initial_schema.sql`) — migration 004 handles the type transition
- `roles` table exists (confirmed from 001 schema) — migration 005 seeds into it
- Git retains full session history — `sessions` table can be pruned aggressively
- dev-skill GitHub repo (`nitekeeper/dev-skill`) is already private — safe to delete

## Open Questions

| Question | Owner | Status |
|---|---|---|
| File system / wiki management for workspace | User + PM | Deferred to next design session |
| Should `sessions.agent_id` be nullable? (PM might not always have an agent row on first run) | Implementer | Resolve in migration 002 |
| Migration 004 data transform: existing INTEGER priority values (0, 1, 2...) map to which TEXT enum? | Implementer | Suggest: 0=low, 1=medium, 2=high, 3=critical |

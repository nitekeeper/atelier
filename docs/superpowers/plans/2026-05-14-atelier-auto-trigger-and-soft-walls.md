# Atelier Auto-Trigger and Soft Walls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Atelier auto-engage on new-work requests via a canonical bootstrap skill exposed through four reinforcing surfaces, and soften phase walls so out-of-phase skill invocations are warned and logged rather than blocked.

**Architecture:** The change spans three layers. (1) Foundation: a new `phase_bypasses` table and a rewritten `workflow.py:check_gate` that returns a `GateResult` rather than raising; (2) Methodology: a single canonical `skills/using-atelier/SKILL.md` containing the trigger contract, Red Flags, phase guidance, dev arc, and bypass procedure — surfaced through a new SessionStart hook, an extension to the existing `session_open.py` PreToolUse hook, a CLAUDE.md template snippet, and per-skill YAML frontmatter on four session-lifecycle skills; (3) Procedure: every dev skill's gate check is updated to handle `GateResult` and the user-confirm-and-log bypass flow.

**Tech Stack:**
- Python 3.11+
- SQLite (via existing `scripts/db.py` connection helper, WAL mode)
- pytest (existing test framework, 189 tests baseline)
- Claude Code SessionStart hook mechanism (new for Atelier)
- Existing PreToolUse hook mechanism (extending `session_open.py`)

**Spec reference:** `docs/superpowers/specs/2026-05-14-atelier-auto-trigger-and-soft-walls-design.md`

---

## File Structure

### New files (8)

| Path | Responsibility |
|---|---|
| `migrations/005_soft_walls.sql` | Create `phase_bypasses` table and index |
| `skills/using-atelier/SKILL.md` | Canonical methodology source: trigger contract, Red Flags, phase guidance, dev arc, bypass procedure |
| `hooks/session_start.py` | SessionStart hook — injects `using-atelier` body as system context |
| `templates/CLAUDE-snippet.md` | Short backup methodology for consumers to paste into their CLAUDE.md |
| `tests/test_using_atelier_skill.py` | Validates canonical file parses correctly |
| `tests/test_session_start_hook.py` | Tests new SessionStart hook |
| `tests/test_phase_bypasses.py` | Tests `phase_bypasses` table operations and `log-bypass` command |
| `tests/test_soft_walls.py` | Tests `check_gate` returns `GateResult`, end-to-end bypass flow |
| `tests/test_skill_bypass_flow.py` | Integration test exercising the bypass pattern through a dev skill |

(File count is 9 with the integration test; spec §4.4 said 8 — small drift in favor of better coverage.)

### Modified files

| Path | Change |
|---|---|
| `scripts/workflow.py` | Rewrite `check_gate` to return `GateResult` dataclass (no longer raises). Add `log-bypass` CLI subcommand. |
| `hooks/session_open.py` | Extend output to append phase-specific guidance (derived from `using-atelier/SKILL.md` phase guidance table). |
| `skills/ingest/SKILL.md` | Add YAML frontmatter with `description: Use when…`. |
| `skills/save/SKILL.md` | Add YAML frontmatter. |
| `skills/load/SKILL.md` | Add YAML frontmatter. |
| `skills/dev-design/SKILL.md` | Update step 1 for new `check_gate` return value. |
| `skills/dev-plan/SKILL.md` | Update step 1 for bypass flow (confirm + log). |
| `skills/dev-tdd/SKILL.md` | Same bypass pattern. |
| `skills/dev-review/SKILL.md` | Same bypass pattern. |
| `skills/dev-security/SKILL.md` | Same bypass pattern. |
| `skills/dev-qa/SKILL.md` | Same bypass pattern. |
| `skills/dev-diagnose/SKILL.md` | Update step 1 (always allowed) + procedure unchanged. |
| `skills/dev-handoff/SKILL.md` | Update step 1 + add bypass surfacing in retro summary. |
| `tests/test_migrations.py` | Extend to cover migration 005. |
| `README.md` | Add "Auto-trigger contract" section; setup instructions for SessionStart hook. |
| `CLAUDE.md` (atelier repo) | Document the canonical-source-plus-four-surfaces architecture. |
| `CHANGELOG.md` | Add v0.2.0 entry. |

### Unchanged (explicitly out of scope for this plan)

`scripts/db.py`, `scripts/projects.py`, `scripts/tasks.py`, `scripts/agents.py`, `scripts/roles.py`, `scripts/documents.py`, `scripts/meetings.py`, `scripts/workspace.py`, `scripts/session.py`, all CRUD/workspace `SKILL.md` files, `migrations/001`–`004`.

---

## Task outline

Tasks are ordered so that each one's dependencies are complete before it starts. Foundation first, canonical methodology next, then surfaces, then procedure updates, then integration tests and documentation.

| # | Task | Layer | Files touched |
|---|---|---|---|
| 1 | Migration 005: `phase_bypasses` table | Foundation | `migrations/005_soft_walls.sql`, `tests/test_migrations.py` |
| 2 | Rewrite `check_gate` to return `GateResult` | Foundation | `scripts/workflow.py`, `tests/test_soft_walls.py` |
| 3 | Add `log-bypass` CLI subcommand | Foundation | `scripts/workflow.py`, `tests/test_phase_bypasses.py` |
| 4 | Write canonical `using-atelier/SKILL.md` | Methodology | `skills/using-atelier/SKILL.md`, `tests/test_using_atelier_skill.py` |
| 5 | New SessionStart hook | Methodology surface | `hooks/session_start.py`, `tests/test_session_start_hook.py` |
| 6 | Extend `session_open.py` with phase guidance | Methodology surface | `hooks/session_open.py`, existing hook test |
| 7 | CLAUDE.md template snippet | Methodology surface | `templates/CLAUDE-snippet.md` |
| 8 | Frontmatter on `ingest`, `save`, `load` | Methodology surface | 3 SKILL.md files |
| 9 | Update unwalled dev skills (`dev-design`, `dev-diagnose`, `dev-handoff`) for new `check_gate` return | Procedure | 3 SKILL.md files |
| 10 | Update walled non-review skills (`dev-plan`, `dev-tdd`) for bypass flow | Procedure | 2 SKILL.md files |
| 11 | Update walled review skills (`dev-review`, `dev-security`, `dev-qa`) for bypass flow | Procedure | 3 SKILL.md files |
| 12 | Add bypass surfacing to `dev-handoff` retro | Procedure | `skills/dev-handoff/SKILL.md` |
| 13 | Integration test: end-to-end bypass flow | Verification | `tests/test_skill_bypass_flow.py` |
| 14 | Documentation: README, CLAUDE.md (atelier), CHANGELOG | Documentation | 3 docs |

14 tasks total. Each task includes its own TDD cycle (failing test → minimal implementation → verify → commit).

---

## Task 1: Migration 005 — `phase_bypasses` table

**Files:**
- Create: `migrations/005_soft_walls.sql`
- Modify: `tests/test_migrations.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_migrations.py`:

```python
def test_migration_005_creates_phase_bypasses_table(tmp_path):
    """phase_bypasses table exists after running migrations through 005."""
    db_path = tmp_path / "memex.db"
    migrate.run_migrations(str(db_path))

    with db.connection(str(db_path)) as conn:
        # Table exists
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='phase_bypasses'"
        ).fetchone()
        assert row is not None, "phase_bypasses table not created"

        # Required columns present
        cols = {r[1] for r in conn.execute("PRAGMA table_info(phase_bypasses)").fetchall()}
        expected = {"id", "project_id", "skill", "current_phase", "required_phase",
                    "bypassed_at", "agent_id", "note"}
        assert expected.issubset(cols), f"missing columns: {expected - cols}"

        # Index present
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='phase_bypasses_project_idx'"
        ).fetchone()
        assert idx is not None, "phase_bypasses_project_idx not created"


def test_migration_005_is_idempotent(tmp_path):
    """Re-running migration 005 on an already-migrated DB is safe."""
    db_path = tmp_path / "memex.db"
    migrate.run_migrations(str(db_path))
    migrate.run_migrations(str(db_path))  # second run — must not raise

    with db.connection(str(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='phase_bypasses'"
        ).fetchone()[0]
        assert count == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd C:/Users/user/Documents/Skills/atelier
PYTHONPATH=. python -m pytest tests/test_migrations.py::test_migration_005_creates_phase_bypasses_table tests/test_migrations.py::test_migration_005_is_idempotent -v
```
Expected: FAIL — both tests fail because migration 005 does not exist yet (no `phase_bypasses` table created).

- [ ] **Step 3: Write the migration SQL**

Create `migrations/005_soft_walls.sql`:

```sql
-- migrations/005_soft_walls.sql
-- Soft walls: add phase_bypasses table for logging out-of-phase skill invocations.
-- Spec: docs/superpowers/specs/2026-05-14-atelier-auto-trigger-and-soft-walls-design.md §3.3

CREATE TABLE IF NOT EXISTS phase_bypasses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    skill           TEXT NOT NULL,
    current_phase   TEXT NOT NULL,
    required_phase  TEXT NOT NULL,
    bypassed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    agent_id        TEXT REFERENCES agents(id),
    note            TEXT
);

CREATE INDEX IF NOT EXISTS phase_bypasses_project_idx ON phase_bypasses(project_id);
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_migrations.py -v
```
Expected: PASS — all migration tests including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add migrations/005_soft_walls.sql tests/test_migrations.py
git commit -m "feat: add migration 005 -- phase_bypasses table for soft walls"
```

---

## Task 2: Rewrite `check_gate` to return `GateResult`

**Files:**
- Modify: `scripts/workflow.py`
- Create: `tests/test_soft_walls.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_soft_walls.py`:

```python
"""Soft walls: check_gate returns GateResult instead of raising."""
from pathlib import Path

import pytest

from scripts import db, migrate, workflow
from scripts.projects import create_project


@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "memex.db"
    migrate.run_migrations(str(db_path))
    return str(db_path)


@pytest.fixture
def project(fresh_db):
    """Create a project; returns (db_path, project_id). Starts at design:open."""
    project_id = create_project(fresh_db, name="test", created_by="test-agent")
    return fresh_db, project_id


def test_check_gate_returns_gate_result_when_allowed(project):
    db_path, project_id = project
    # dev:design has no gate
    result = workflow.check_gate(db_path, project_id, "dev:design")
    assert result.allowed is True
    assert result.current_phase == "design:open"
    assert result.required_phase is None
    assert "no gate" in result.reason.lower()


def test_check_gate_returns_gate_result_when_phase_satisfies(project):
    db_path, project_id = project
    # Advance to design:approved so dev:plan is allowed
    workflow.advance_phase(db_path, project_id, "design:approved")
    result = workflow.check_gate(db_path, project_id, "dev:plan")
    assert result.allowed is True
    assert result.current_phase == "design:approved"
    assert result.required_phase == "design:approved"


def test_check_gate_does_not_raise_on_mismatch(project):
    """The old behavior raised WorkflowError. New behavior returns GateResult."""
    db_path, project_id = project
    # Project is at design:open; dev:plan requires design:approved
    result = workflow.check_gate(db_path, project_id, "dev:plan")
    assert result.allowed is False
    assert result.current_phase == "design:open"
    assert result.required_phase == "design:approved"
    assert "design:open" in result.reason
    assert "design:approved" in result.reason
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_soft_walls.py -v
```
Expected: FAIL — `check_gate` currently raises `WorkflowError` instead of returning a `GateResult`. Also `GateResult` does not exist as an importable symbol.

- [ ] **Step 3: Rewrite `check_gate` in `scripts/workflow.py`**

Add at top of file (after existing imports):

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    """Result of a phase gate check.

    `allowed=True` means the skill may proceed immediately.
    `allowed=False` means a soft wall is hit; caller should ask user
    to confirm bypass and then call `log_bypass`.
    """
    allowed: bool
    current_phase: str
    required_phase: str | None
    reason: str
```

Replace the existing `check_gate` function with:

```python
def check_gate(db_path: str, project_id: int, skill: str) -> GateResult:
    """Check whether `skill` is in-phase for `project_id`.

    Returns a GateResult describing the outcome. Does NOT raise on mismatch.
    Callers decide whether to proceed (typically: confirm with user, log bypass,
    then proceed).
    """
    with db.connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT required_phase FROM skill_gates WHERE skill = ?", (skill,)
        )
        row = cursor.fetchone()
    current = get_phase(db_path, project_id)

    # No row, or row's required_phase is NULL -> no gate
    if row is None or row[0] is None:
        return GateResult(
            allowed=True,
            current_phase=current,
            required_phase=None,
            reason="No gate configured for this skill",
        )

    required = row[0]
    if current == required:
        return GateResult(
            allowed=True,
            current_phase=current,
            required_phase=required,
            reason=f"Project at '{current}' satisfies the gate",
        )

    return GateResult(
        allowed=False,
        current_phase=current,
        required_phase=required,
        reason=(
            f"Project is at '{current}', this skill normally requires '{required}'. "
            "Bypass is available — confirm with user before proceeding."
        ),
    )
```

Update the CLI handler for `check-gate` (replace existing block):

```python
if cmd == "check-gate":
    project_id = int(sys.argv[2])
    skill = sys.argv[3]
    result = check_gate(db_path, project_id, skill)
    print(json.dumps({
        "allowed": result.allowed,
        "current_phase": result.current_phase,
        "required_phase": result.required_phase,
        "reason": result.reason,
    }))
    sys.exit(0)  # always 0 -- "not allowed" is no longer an error
```

Add `import json` to the script if not already imported.

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_soft_walls.py -v
```
Expected: PASS — three new tests pass.

Also run the full suite to confirm no regression:

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: existing tests pass except any that depended on `check_gate` raising `WorkflowError`. **If any existing test fails because it expected the old raising behavior, fix it now**: those tests should be updated to call `check_gate` and assert on the `GateResult` fields. List failing tests and update them in this same task.

- [ ] **Step 5: Commit**

```bash
git add scripts/workflow.py tests/test_soft_walls.py
# Plus any existing test files updated for the new return contract
git commit -m "refactor: workflow.check_gate returns GateResult instead of raising"
```

---

## Task 3: Add `log-bypass` CLI subcommand

**Files:**
- Modify: `scripts/workflow.py`
- Create: `tests/test_phase_bypasses.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_phase_bypasses.py`:

```python
"""Tests for the phase_bypasses table and the log-bypass workflow command."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import db, migrate, workflow
from scripts.projects import create_project


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path):
    db_path = tmp_path / "memex.db"
    migrate.run_migrations(str(db_path))
    pid = create_project(str(db_path), name="test", created_by="test-agent")
    return str(db_path), pid


def test_log_bypass_writes_row(project):
    db_path, pid = project
    bypass_id = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
        agent_id="test-agent", note="testing soft wall bypass",
    )
    assert isinstance(bypass_id, int) and bypass_id > 0

    with db.connection(db_path) as conn:
        row = conn.execute(
            "SELECT project_id, skill, current_phase, required_phase, agent_id, note "
            "FROM phase_bypasses WHERE id = ?", (bypass_id,)
        ).fetchone()
    assert row == (pid, "dev:plan", "design:open", "design:approved",
                   "test-agent", "testing soft wall bypass")


def test_log_bypass_idempotent_within_one_minute(project):
    """Same (project, skill, current, required) inside 60 seconds — only one row."""
    db_path, pid = project
    first = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
    )
    second = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
    )
    # Idempotency returns the existing row id rather than creating a new one
    assert first == second

    with db.connection(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = ?", (pid,)
        ).fetchone()[0]
    assert count == 1


def test_log_bypass_cli_writes_row(project):
    db_path, pid = project
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "workflow.py"),
         db_path, "log-bypass", str(pid), "dev:plan",
         "design:open", "design:approved",
         "--agent", "test-agent", "--note", "from CLI"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    with db.connection(db_path) as conn:
        row = conn.execute(
            "SELECT skill, agent_id, note FROM phase_bypasses WHERE project_id = ?",
            (pid,),
        ).fetchone()
    assert row == ("dev:plan", "test-agent", "from CLI")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_phase_bypasses.py -v
```
Expected: FAIL — `workflow.log_bypass` does not exist; the CLI does not handle `log-bypass`.

- [ ] **Step 3: Add `log_bypass` function and CLI handler**

Add to `scripts/workflow.py` (after `check_gate`):

```python
def log_bypass(
    db_path: str,
    project_id: int,
    skill: str,
    current_phase: str,
    required_phase: str,
    agent_id: str | None = None,
    note: str | None = None,
) -> int:
    """Log a soft-wall bypass to phase_bypasses.

    Idempotent: if a row with the same (project, skill, current_phase,
    required_phase) was written within the last 60 seconds, returns that
    row's id instead of inserting a new one.
    """
    with db.connection(db_path) as conn:
        existing = conn.execute(
            """SELECT id FROM phase_bypasses
               WHERE project_id = ? AND skill = ?
                 AND current_phase = ? AND required_phase = ?
                 AND bypassed_at >= datetime('now', '-60 seconds')
               LIMIT 1""",
            (project_id, skill, current_phase, required_phase),
        ).fetchone()
        if existing is not None:
            return existing[0]

        cursor = conn.execute(
            """INSERT INTO phase_bypasses
                 (project_id, skill, current_phase, required_phase, agent_id, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project_id, skill, current_phase, required_phase, agent_id, note),
        )
        conn.commit()
        return cursor.lastrowid
```

Add CLI handler in the `if __name__ == "__main__":` block (mirror existing subcommand style):

```python
elif cmd == "log-bypass":
    project_id = int(sys.argv[2])
    skill = sys.argv[3]
    current_phase = sys.argv[4]
    required_phase = sys.argv[5]
    agent_id = None
    note = None
    i = 6
    while i < len(sys.argv):
        if sys.argv[i] == "--agent":
            agent_id = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--note":
            note = sys.argv[i + 1]
            i += 2
        else:
            print(f"Unknown argument: {sys.argv[i]}", file=sys.stderr)
            sys.exit(1)
    bypass_id = log_bypass(
        db_path, project_id, skill, current_phase, required_phase,
        agent_id=agent_id, note=note,
    )
    print(json.dumps({"bypass_id": bypass_id}))
    sys.exit(0)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_phase_bypasses.py -v
```
Expected: PASS — three tests pass.

Run the full suite:

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: full suite passes including new bypass tests, soft walls tests, migration tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/workflow.py tests/test_phase_bypasses.py
git commit -m "feat: add workflow.log_bypass function and log-bypass CLI subcommand"
```

---

## Task 4: Write canonical `skills/using-atelier/SKILL.md`

**Files:**
- Create: `skills/using-atelier/SKILL.md`
- Create: `tests/test_using_atelier_skill.py`

This task writes the canonical methodology source. Five structured sections plus YAML frontmatter. The file is the single source of truth that all four surface mechanisms derive from.

- [ ] **Step 1: Write the failing test**

Create `tests/test_using_atelier_skill.py`:

```python
"""Validates skills/using-atelier/SKILL.md is parseable and complete."""
import re
from pathlib import Path

import yaml

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "using-atelier" / "SKILL.md"


def _read_skill():
    text = SKILL_PATH.read_text(encoding="utf-8")
    # Frontmatter is delimited by --- on lines by themselves
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert m is not None, "SKILL.md missing YAML frontmatter delimited by ---"
    return yaml.safe_load(m.group(1)), m.group(2)


def test_skill_file_exists():
    assert SKILL_PATH.exists(), f"{SKILL_PATH} does not exist"


def test_frontmatter_has_required_keys():
    frontmatter, _ = _read_skill()
    assert "name" in frontmatter
    assert frontmatter["name"] == "using-atelier"
    assert "description" in frontmatter
    assert "Use when" in frontmatter["description"]


def test_body_has_required_sections():
    _, body = _read_skill()
    required_sections = [
        "## Trigger contract",
        "## Red Flags",
        "## Phase guidance",
        "## Dev arc",
        "## Bypass procedure",
    ]
    for section in required_sections:
        assert section in body, f"missing section: {section}"


def test_phase_guidance_table_has_all_phases():
    """Every non-terminal phase from migrations/003 must appear in the phase
    guidance table."""
    _, body = _read_skill()
    # The phase guidance section contains a markdown table with phase names
    # in the first column. Extract the section.
    phase_section_match = re.search(
        r"## Phase guidance\n(.*?)(?=\n## )", body, re.DOTALL,
    )
    assert phase_section_match, "Phase guidance section not found or improperly closed"
    phase_block = phase_section_match.group(1)

    expected_phases = {
        "design:open", "design:approved",
        "plan:open", "plan:approved",
        "tdd:red", "tdd:green", "tdd:clean",
        "review:open", "review:changes-requested", "review:approved",
        "security:open", "security:approved",
        "qa:open", "qa:approved",
        "diagnose:open", "diagnose:resolved",
        "handoff:complete",
    }
    for phase in expected_phases:
        # Phase name appears as `phase:name` (backtick-quoted in the table)
        assert f"`{phase}`" in phase_block, f"phase '{phase}' missing from phase guidance"


def test_dev_arc_references_canonical_flow():
    _, body = _read_skill()
    arc_section_match = re.search(r"## Dev arc\n(.*?)(?=\n## )", body, re.DOTALL)
    assert arc_section_match, "Dev arc section not found or improperly closed"
    arc = arc_section_match.group(1)
    # The arc must mention every dev phase in canonical order
    for phase in ["design", "plan", "tdd", "review", "security", "qa", "handoff"]:
        assert phase in arc, f"dev arc missing '{phase}'"


def test_trigger_contract_describes_three_routings():
    _, body = _read_skill()
    contract = re.search(r"## Trigger contract\n(.*?)(?=\n## )", body, re.DOTALL).group(1)
    # Must describe three routings: full arc, diagnose, direct
    assert "Full Atelier arc" in contract or "full arc" in contract.lower()
    assert "diagnose" in contract.lower()
    assert "directly" in contract.lower() or "without" in contract.lower()


def test_red_flags_table_present():
    _, body = _read_skill()
    red_flags = re.search(r"## Red Flags\n(.*?)(?=\n## )", body, re.DOTALL).group(1)
    # The table must have at least 5 rows (entries from spec §2.3)
    table_rows = [r for r in red_flags.split("\n") if r.strip().startswith("|") and "---" not in r]
    # Strip header rows; expect substantive rows >= 5
    assert len(table_rows) >= 6, f"Red Flags table needs more rows; found {len(table_rows)}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_using_atelier_skill.py -v
```
Expected: FAIL — `skills/using-atelier/SKILL.md` does not exist.

If `yaml` is not installed, also expect an import error. If so, add `PyYAML>=6.0` to `requirements.txt` (or `requirements-dev.txt` if test-only is preferred) and `pip install -r requirements.txt`.

- [ ] **Step 3: Add PyYAML if missing**

Check `requirements.txt`:

```bash
grep -i yaml requirements.txt requirements-dev.txt 2>/dev/null
```

If no match, append `PyYAML>=6.0` to `requirements-dev.txt` and `pip install -r requirements-dev.txt`.

- [ ] **Step 4: Write the canonical skill file**

Create `skills/using-atelier/SKILL.md`:

````markdown
---
name: using-atelier
description: Use when starting any session in a project that uses Atelier — establishes the trigger contract for new-work requests and the soft-wall bypass procedure.
---

# using-atelier

Atelier is a workspace and methodology for a human developer collaborating with one or more AI agents on a software project. This skill defines the trigger contract every session follows and the bypass procedure for soft phase walls.

## Trigger contract

On every user message, before responding:

1. **Mid-arc rule.** If a project is active and its phase is not `handoff:complete`, continue the current arc. Do NOT ask. Proceed with the phase-recommended skill (see Phase guidance) or with the user's explicit request.
2. **No-fire rule.** If the message is a question, exploration, read-only request, or trivial edit (see Red Flags), handle directly without asking.
3. **Ask gate.** If the message describes new development work, ask the user one of three routings:
   - **(a) Full Atelier arc** — invoke `project:create`, then `dev:design`. Routes through design → plan → tdd → review → security → qa → handoff with soft walls.
   - **(b) Bug fix** — invoke `dev:diagnose` against the active project. Captures pre-diagnose phase, writes regression test first, restores phase on resolution.
   - **(c) Handle directly** — do the work without Atelier orchestration. No project created, no phase tracked.

Wait for an explicit user response. Default to (a) if the user says "yes" without specifying.

## Red Flags

| Thought | Reality |
|---|---|
| "User just wants a quick fix" | Quick fixes still go through option (b). Ask. |
| "This is too small to need design" | Ask. User can pick option (c). |
| "User is asking a question, no need to ask" | Correct — questions don't fire. Only work requests fire. |
| "Project is already active, no need to ask" | Correct — don't re-ask mid-arc. Continue current phase. |
| "User said 'how do I X' so it's a question" | Verify: are they asking how, or asking the agent to do it? Latter fires. |
| "User said 'rename X to Y' — it's a tiny edit" | Tiny mechanical edits do not fire. Substantive renames (refactors affecting >5 files) fire. |
| "Refactor isn't new work" | Substantive refactors are new work. They get specs and reviews. Ask. |

**Firing patterns (examples):**
- "I want to add X" → fires
- "Build a system that does Y" → fires
- "The bug in Z is back" → fires (option b recommended)
- "Refactor the auth module" → fires
- "How does this codebase handle X?" → does not fire (question)
- "Show me the file at path Y" → does not fire (read-only)
- "Fix the typo on line 42" → does not fire (trivial edit)
- "List the open tasks" → does not fire (CRUD)

## Phase guidance

| Phase | Recommended next action | Skill |
|---|---|---|
| `design:open` | Continue grilling. Do not write code yet. | `dev:design` |
| `design:approved` | Draft the implementation plan. | `dev:plan` |
| `plan:open` | Continue refining the plan with the user. | `dev:plan` |
| `plan:approved` | Write the first failing test. | `dev:tdd-red` |
| `tdd:red` | Write minimal implementation to make tests pass. | `dev:tdd` |
| `tdd:green` | Refactor with tests still passing. | `dev:tdd` |
| `tdd:clean` | Continue TDD (new test) or advance to review. | `dev:tdd` or `dev:review` |
| `review:open` | Address findings or mark as approved. | `dev:review` |
| `review:changes-requested` | Apply requested changes, then re-review. | `dev:review` |
| `review:approved` | Run security review. | `dev:security` |
| `security:open` | Apply security findings or mark approved. | `dev:security` |
| `security:approved` | Run QA review. | `dev:qa` |
| `qa:open` | Address QA findings or mark approved. | `dev:qa` |
| `qa:approved` | Close out the project. | `dev:handoff` |
| `diagnose:open` | Reproduce the bug, write regression test, fix root cause. | `dev:diagnose` |
| `diagnose:resolved` | Restore to pre-diagnose phase. | `dev:diagnose` (final steps) |
| `handoff:complete` | Project is closed. New work requires a new project. | — |

## Dev arc

The canonical Atelier development flow:

```
design → plan → tdd (red ⇄ green ⇄ clean) → review → security → qa → handoff
              ↑
              └── diagnose (entered from any non-terminal phase, restored on resolve)
```

All transitions are tracked in `memex.db` (`projects.phase` column). Transitions are validated by `scripts/workflow.py advance` against the `phase_transitions` table. Skills no longer block on out-of-phase invocation — instead they apply the Bypass procedure below.

## Bypass procedure

Every dev skill's step 1 follows this pattern:

1. Call `python scripts/workflow.py <db_path> check-gate <project_id> <skill>`. Parse the JSON output. The fields are: `allowed` (bool), `current_phase` (str), `required_phase` (str | null), `reason` (str).
2. **If `allowed` is true:** proceed with the skill's procedure.
3. **If `allowed` is false:**
   - Display to the user: *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*
   - On **yes:** call `python scripts/workflow.py <db_path> log-bypass <project_id> <skill> <current_phase> <required_phase>` (optionally with `--agent <agent_id>` and `--note "<reason>"`), then proceed with the skill's procedure.
   - On **no:** stop. Tell the user: *"Advance to `<required_phase>` first (run `python scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*

Bypass entries are recorded in the `phase_bypasses` table and surfaced by `dev:handoff` during retrospective.
````

- [ ] **Step 5: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_using_atelier_skill.py -v
```
Expected: PASS — all parseability tests pass.

- [ ] **Step 6: Commit**

```bash
git add skills/using-atelier/SKILL.md tests/test_using_atelier_skill.py
git add requirements-dev.txt  # if PyYAML was added
git commit -m "feat: add canonical using-atelier skill with trigger contract and bypass procedure"
```

---

## Task 5: New SessionStart hook (`hooks/session_start.py`)

**Files:**
- Create: `hooks/session_start.py`
- Create: `tests/test_session_start_hook.py`

The hook reads `skills/using-atelier/SKILL.md` and prints its body to stdout, which Claude Code injects as system context for the new session.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_start_hook.py`:

```python
"""Tests for the SessionStart hook."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "session_start.py"


def test_hook_outputs_skill_body():
    """Hook stdout contains the body of using-atelier/SKILL.md."""
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"hook failed; stderr: {result.stderr}"
    # Body must contain the canonical sections
    for section in ["## Trigger contract", "## Red Flags",
                    "## Phase guidance", "## Dev arc", "## Bypass procedure"]:
        assert section in result.stdout, f"missing section in hook output: {section}"


def test_hook_does_not_emit_frontmatter():
    """The frontmatter delimiters and YAML keys should not appear in injected context."""
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=REPO_ROOT,
    )
    # The hook strips the frontmatter block before printing
    assert "---\nname: using-atelier" not in result.stdout
    assert not result.stdout.lstrip().startswith("---")


def test_hook_exits_zero_when_skill_missing(tmp_path, monkeypatch):
    """If using-atelier/SKILL.md is missing, hook exits 0 (does not block session)."""
    # Run hook from a temp directory where skills/ is empty
    (tmp_path / "skills").mkdir()
    (tmp_path / "hooks").mkdir()
    target_hook = tmp_path / "hooks" / "session_start.py"
    target_hook.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(target_hook)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=tmp_path,
    )
    # Must NOT block session even if the canonical file is missing
    assert result.returncode == 0, f"hook returned non-zero on missing skill; stderr: {result.stderr}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_session_start_hook.py -v
```
Expected: FAIL — `hooks/session_start.py` does not exist.

- [ ] **Step 3: Write the hook**

Create `hooks/session_start.py`:

```python
#!/usr/bin/env python3
"""
Atelier SessionStart hook.

Reads skills/using-atelier/SKILL.md and prints its body (frontmatter stripped)
to stdout. Claude Code injects stdout as system context for the new session,
giving the agent the trigger contract and bypass procedure from the first
user message.

Hook spec: never block a session. On any error, print nothing and exit 0.

Install: add to .claude/settings.json:
  {
    "hooks": {
      "SessionStart": [
        {"matcher": "", "hooks": [
          {"type": "command",
           "command": "python /path/to/atelier/hooks/session_start.py"}
        ]}
      ]
    }
  }
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
_SKILL_PATH = _HOOK_DIR.parent / "skills" / "using-atelier" / "SKILL.md"


def main() -> int:
    try:
        if not _SKILL_PATH.exists():
            # Canonical file missing -- silently exit 0 (never block a session).
            return 0
        text = _SKILL_PATH.read_text(encoding="utf-8")
        # Strip YAML frontmatter if present (--- delimited block at file start)
        match = re.match(r"^---\n.*?\n---\n(.*)$", text, re.DOTALL)
        body = match.group(1) if match else text
        sys.stdout.write(body)
        return 0
    except Exception:
        # Per hook spec: never raise out of a hook.
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

Make it executable (matches existing `session_open.py` convention):

```bash
chmod +x hooks/session_start.py
```

(On Windows, the `chmod` is a no-op; the `python /path` invocation in `.claude/settings.json` runs the script regardless of the executable bit.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_session_start_hook.py -v
```
Expected: PASS — three tests pass.

Run the full suite to confirm no regression:

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add hooks/session_start.py tests/test_session_start_hook.py
git commit -m "feat: add SessionStart hook to inject using-atelier as system context"
```

---

## Task 6: Extend `hooks/session_open.py` with phase guidance

**Files:**
- Modify: `hooks/session_open.py`
- Modify: `tests/test_session_open_hook.py` (extend the existing test file; if it doesn't exist, create it under this filename)

The existing hook announces the project's current phase. The extension appends a one-line "Recommended next action" derived from the phase guidance table in `using-atelier/SKILL.md`. Single-sourced: any change to the table updates the hook output automatically.

- [ ] **Step 1: Write the failing test**

Append to (or create) `tests/test_session_open_hook.py`:

```python
"""Tests for the session_open PreToolUse hook (phase guidance extension)."""
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import db, migrate, workflow
from scripts.projects import create_project

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "session_open.py"


@pytest.fixture
def project_at_phase(tmp_path):
    """Factory: returns a callable producing (db_path, project_id) at any phase."""
    def _make(phase: str):
        db_path = tmp_path / "memex.db"
        migrate.run_migrations(str(db_path))
        pid = create_project(str(db_path), name="t", created_by="agent")
        if phase != "design:open":
            # Use force-phase to bypass the transition graph for test setup
            workflow.force_phase(str(db_path), pid, phase)
        # Write .ai/active_project so the hook finds it
        ai_dir = tmp_path / ".ai"
        ai_dir.mkdir(exist_ok=True)
        (ai_dir / "active_project").write_text(str(pid), encoding="utf-8")
        return str(db_path), pid, tmp_path
    return _make


@pytest.mark.parametrize("phase,expected_skill", [
    ("design:open", "dev:design"),
    ("plan:approved", "dev:tdd"),
    ("tdd:clean", "dev:review"),
    ("review:approved", "dev:security"),
    ("qa:approved", "dev:handoff"),
])
def test_hook_appends_phase_guidance(project_at_phase, phase, expected_skill):
    db_path, pid, cwd = project_at_phase(phase)
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True, text=True, encoding="utf-8", cwd=cwd,
    )
    assert result.returncode == 0
    # Output mentions both the phase and the recommended skill
    assert phase in result.stdout
    assert expected_skill in result.stdout


def test_hook_handles_missing_using_atelier_gracefully(project_at_phase, monkeypatch):
    """If using-atelier/SKILL.md is missing, hook still announces phase."""
    db_path, pid, cwd = project_at_phase("design:open")
    # Temporarily rename the skill file
    skill_path = REPO_ROOT / "skills" / "using-atelier" / "SKILL.md"
    backup = skill_path.with_suffix(".md.bak")
    try:
        skill_path.rename(backup)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            capture_output=True, text=True, encoding="utf-8", cwd=cwd,
        )
        # Hook still completes; phase announcement still happens
        assert result.returncode == 0
        assert "design:open" in result.stdout
    finally:
        if backup.exists():
            backup.rename(skill_path)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_session_open_hook.py -v
```
Expected: FAIL — phase-guidance lines aren't appended yet; tests checking for `dev:design`, `dev:tdd-red`, etc. in hook output fail.

- [ ] **Step 3: Extend `hooks/session_open.py`**

Add a helper at the top of the file (after existing imports):

```python
import re

_USING_ATELIER_PATH = _HOOK_DIR.parent / "skills" / "using-atelier" / "SKILL.md"


def get_phase_guidance(phase: str) -> str | None:
    """Read the phase guidance table from using-atelier/SKILL.md and return the
    line for `phase`, formatted for hook output. Returns None on any failure
    (missing file, table not found, phase not present) -- caller should not
    block the session on a None return."""
    try:
        if not _USING_ATELIER_PATH.exists():
            return None
        text = _USING_ATELIER_PATH.read_text(encoding="utf-8")
        section = re.search(r"## Phase guidance\n(.*?)(?=\n## )", text, re.DOTALL)
        if not section:
            return None
        # Each table row: | `phase` | recommendation | `skill` |
        # Look for the exact phase in backticks at the start of a row
        row_pattern = re.compile(
            rf"\|\s*`{re.escape(phase)}`\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|",
        )
        match = row_pattern.search(section.group(1))
        if not match:
            return None
        recommendation = match.group(1).strip()
        skill = match.group(2).strip()
        return f"Recommended next action: {recommendation} ({skill})"
    except Exception:
        return None
```

Find the existing function that emits the phase announcement to stdout (look for the `print` or output-formatting call that follows `fetch_latest_session`). Immediately after the existing phase line, append:

```python
guidance = get_phase_guidance(current_phase)
if guidance:
    print(guidance)
```

(Adjust variable names to match the existing code: `current_phase` should be whatever variable holds the phase string in the existing function.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_session_open_hook.py -v
```
Expected: PASS — five parametrized tests + the missing-file test all pass.

Run the full suite:

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add hooks/session_open.py tests/test_session_open_hook.py
git commit -m "feat: extend session_open hook to append phase guidance from using-atelier"
```

---

## Task 7: CLAUDE.md template snippet

**Files:**
- Create: `templates/CLAUDE-snippet.md`

A short backup methodology the consumer pastes into their target project's `CLAUDE.md`. Acts as the fallback in case the SessionStart hook isn't installed or doesn't run.

- [ ] **Step 1: Write the snippet**

Create `templates/CLAUDE-snippet.md`:

```markdown
<!-- Atelier methodology — paste into your project's CLAUDE.md -->
<!-- Source of truth: skills/using-atelier/SKILL.md in the Atelier install -->

## Atelier methodology

This project uses Atelier for development workflow.

**On every user message, before responding:**

1. **Mid-arc rule.** If a project is active and its phase is not `handoff:complete`, continue the current arc. Do NOT ask. Use the phase-recommended skill from `using-atelier/SKILL.md`.
2. **No-fire rule.** Questions, exploration, read-only requests, and trivial edits are handled directly without asking.
3. **Ask gate.** New development work triggers a three-routing ask:
   - **(a) Full Atelier arc** — `project:create` then `dev:design` → plan → tdd → review → security → qa → handoff
   - **(b) Bug fix** — `dev:diagnose` (captures pre-diagnose phase, restores on resolve)
   - **(c) Handle directly** — no project, no phase tracking

**Soft walls.** Phase gates are recommendations, not blocks. When a dev skill detects an out-of-phase invocation, it asks the user to confirm a bypass, then logs the bypass to `phase_bypasses` for retrospective.

**Full methodology:** `skills/using-atelier/SKILL.md` in the Atelier install.
**Phase state:** `.ai/memex.db` `projects.phase`.
**Phase gates:** `scripts/workflow.py check-gate <project_id> <skill>` (returns JSON; never blocks).
```

- [ ] **Step 2: Verify the snippet renders correctly**

```bash
cat templates/CLAUDE-snippet.md
```
Expected: the snippet displays as a coherent markdown document with three numbered routing options and a "Soft walls" paragraph.

No test step — this file is documentation, not code. The Task 4 parseability test indirectly covers cross-references (the snippet points to `skills/using-atelier/SKILL.md`, which Task 4's test verifies exists and is well-formed).

- [ ] **Step 3: Commit**

```bash
git add templates/CLAUDE-snippet.md
git commit -m "feat: add CLAUDE.md template snippet for consumer projects"
```

---

## Task 8: Frontmatter on session skills (`ingest`, `save`, `load`)

**Files:**
- Modify: `skills/ingest/SKILL.md`
- Modify: `skills/save/SKILL.md`
- Modify: `skills/load/SKILL.md`

Per spec §2.6, only four skills get YAML frontmatter: `using-atelier` (already done in Task 4) plus the three session-lifecycle skills. Dev-workflow and CRUD skills do NOT get frontmatter.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_skill_frontmatter.py`:

```python
"""Verify session-lifecycle skills have YAML frontmatter with required keys."""
import re
from pathlib import Path

import yaml
import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@pytest.mark.parametrize("skill_name", ["ingest", "save", "load"])
def test_session_skill_has_frontmatter(skill_name):
    path = SKILLS_DIR / skill_name / "SKILL.md"
    assert path.exists(), f"{path} does not exist"
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, f"{skill_name}: SKILL.md missing YAML frontmatter delimited by ---"
    data = yaml.safe_load(m.group(1))
    assert data.get("name") == skill_name
    assert "description" in data
    assert "Use when" in data["description"], (
        f"{skill_name}: description must start with 'Use when…' trigger phrasing"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_session_skill_frontmatter.py -v
```
Expected: FAIL — `ingest`, `save`, `load` SKILL.md files have no frontmatter.

- [ ] **Step 3: Add frontmatter to each skill**

For each file, prepend a YAML block at the very top (before the existing `# <skill>` heading). Do not change any existing content.

`skills/ingest/SKILL.md`, prepend:

```yaml
---
name: ingest
description: Use when starting a new session and the agent needs to load prior session state and active project context.
---

```

`skills/save/SKILL.md`, prepend:

```yaml
---
name: save
description: Use when ending a session or at a meaningful checkpoint — captures session state for the next resume.
---

```

`skills/load/SKILL.md`, prepend:

```yaml
---
name: load
description: Use when resuming work mid-arc and needing the latest session context loaded into the conversation.
---

```

Note: each block ends with one blank line, then the existing `# ingest` / `# save` / `# load` heading and the rest of the file continues unchanged.

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_session_skill_frontmatter.py -v
```
Expected: PASS — three parametrized tests pass.

Run the full suite:

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add skills/ingest/SKILL.md skills/save/SKILL.md skills/load/SKILL.md \
        tests/test_session_skill_frontmatter.py
git commit -m "feat: add YAML frontmatter to session-lifecycle skills (ingest, save, load)"
```

---

## Task 9: Update unwalled dev skills for new `check_gate` return value

**Files:**
- Modify: `skills/dev-design/SKILL.md`
- Modify: `skills/dev-diagnose/SKILL.md`
- Modify: `skills/dev-handoff/SKILL.md`

These three skills have no phase gate (`required_phase` is `NULL` in `skill_gates`). Step 1 still calls `check-gate` (for consistency) but `allowed` is always `true`, so there's no bypass branch — just parse the JSON, confirm `allowed`, and proceed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_unwalled_skill_step1.py`:

```python
"""Verify unwalled dev skills' SKILL.md step 1 mentions the new check-gate JSON contract."""
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

UNWALLED = ["dev-design", "dev-diagnose", "dev-handoff"]


@pytest.mark.parametrize("skill", UNWALLED)
def test_step_1_uses_json_check_gate(skill):
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # New contract: step 1 mentions parsing JSON output and the `allowed` field
    assert "check-gate" in text, f"{skill}: step 1 must invoke check-gate"
    assert "JSON" in text or "json" in text, (
        f"{skill}: step 1 must reference JSON output of check-gate"
    )
    assert "allowed" in text, f"{skill}: step 1 must reference the `allowed` field"


@pytest.mark.parametrize("skill", UNWALLED)
def test_step_1_does_not_describe_bypass_branch(skill):
    """Unwalled skills don't need the user-confirm-bypass branch because they
    are always allowed. Including it would confuse the agent."""
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # The bypass branch text from the walled-skill pattern must NOT appear
    assert "Proceed anyway" not in text, (
        f"{skill}: should not describe bypass prompt -- it's never reached"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_unwalled_skill_step1.py -v
```
Expected: FAIL — current SKILL.md files don't reference JSON output or `allowed`.

- [ ] **Step 3: Update each SKILL.md**

For each of `dev-design`, `dev-diagnose`, `dev-handoff`, replace the existing step 1 of the **Procedure** section with the following block. Adjust the skill name (`dev:design` / `dev:diagnose` / `dev:handoff`) per file.

```markdown
1. Check the phase gate:
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> <skill_name>
   ```
   Parse the JSON output `{"allowed": bool, "current_phase": str, "required_phase": str|null, "reason": str}`.
   For this skill `allowed` is always `true` (no gate configured). Record `current_phase` for later use, then proceed to the next step.
   - If the project does not exist, stop and tell the user to create one first with `project:create`.
```

For `dev:diagnose` specifically, the recorded `current_phase` becomes `pre_diagnose_phase` (already used by the existing procedure step 2). Add a line: "*Note: the `current_phase` from check-gate is recorded as `<pre_diagnose_phase>` for restoration on resolve (step 13).*"

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_unwalled_skill_step1.py -v
```
Expected: PASS — all parametrized tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/dev-design/SKILL.md skills/dev-diagnose/SKILL.md skills/dev-handoff/SKILL.md \
        tests/test_unwalled_skill_step1.py
git commit -m "refactor: update unwalled dev skills for new check-gate JSON contract"
```

---

## Task 10: Update walled non-review skills for bypass flow

**Files:**
- Modify: `skills/dev-plan/SKILL.md`
- Modify: `skills/dev-tdd/SKILL.md`

These two skills are gated (`dev:plan` requires `design:approved`; `dev:tdd` requires `plan:approved`). Step 1 must check the gate, and if not allowed, prompt the user to confirm a bypass before proceeding.

- [ ] **Step 1: Write the failing test**

Create `tests/test_walled_skill_step1.py`:

```python
"""Verify walled dev skills' SKILL.md step 1 implements the full bypass-flow pattern."""
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

WALLED_NON_REVIEW = ["dev-plan", "dev-tdd"]


@pytest.mark.parametrize("skill", WALLED_NON_REVIEW)
def test_step_1_implements_bypass_flow(skill):
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # Must reference all four parts of the bypass pattern:
    assert "check-gate" in text, f"{skill}: must call check-gate"
    assert "allowed" in text, f"{skill}: must reference the allowed field"
    assert "Proceed anyway" in text, f"{skill}: must include the bypass prompt"
    assert "log-bypass" in text, f"{skill}: must call log-bypass on confirmed bypass"


@pytest.mark.parametrize("skill", WALLED_NON_REVIEW)
def test_step_1_handles_user_no(skill):
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # The "no" branch must tell the user how to advance phase
    assert "advance" in text.lower(), (
        f"{skill}: must tell user how to advance phase on bypass=no"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_walled_skill_step1.py -v
```
Expected: FAIL — current SKILL.md files don't implement the bypass flow.

- [ ] **Step 3: Update each SKILL.md**

For each of `dev-plan` and `dev-tdd`, replace step 1 of the **Procedure** section with the following block. Use the corresponding skill name (`dev:plan` or `dev:tdd`) where indicated.

```markdown
1. Check the phase gate:
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> <skill_name>
   ```
   Parse the JSON output: `{"allowed": bool, "current_phase": str, "required_phase": str, "reason": str}`.

   **If `allowed` is `true`**: record `current_phase` and proceed to the next step.

   **If `allowed` is `false`** (soft wall): ask the user:

   > *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*

   - On **yes**: run:
     ```
     python atelier/scripts/workflow.py <db_path> log-bypass <project_id> <skill_name> <current_phase> <required_phase>
     ```
     Optionally append `--agent <agent_id>` and `--note "<reason>"`. Then proceed to the next step.
   - On **no**: stop. Tell the user:
     > *"Advance to `<required_phase>` first (run `python atelier/scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_walled_skill_step1.py -v
```
Expected: PASS — four parametrized assertions across two skills pass.

Run the full suite:

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add skills/dev-plan/SKILL.md skills/dev-tdd/SKILL.md \
        tests/test_walled_skill_step1.py
git commit -m "refactor: walled non-review skills implement bypass flow"
```

---

## Task 11: Update walled review skills for bypass flow

**Files:**
- Modify: `skills/dev-review/SKILL.md`
- Modify: `skills/dev-security/SKILL.md`
- Modify: `skills/dev-qa/SKILL.md`

Same bypass pattern as Task 10, applied to the three review skills.

- [ ] **Step 1: Extend the existing test**

Append to `tests/test_walled_skill_step1.py`:

```python
WALLED_REVIEWS = ["dev-review", "dev-security", "dev-qa"]


@pytest.mark.parametrize("skill", WALLED_REVIEWS)
def test_review_skill_step_1_implements_bypass_flow(skill):
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "check-gate" in text
    assert "allowed" in text
    assert "Proceed anyway" in text
    assert "log-bypass" in text


@pytest.mark.parametrize("skill", WALLED_REVIEWS)
def test_review_skill_step_1_handles_user_no(skill):
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "advance" in text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_walled_skill_step1.py -v
```
Expected: FAIL — the three review skills don't implement the bypass flow yet.

- [ ] **Step 3: Update each review skill**

For each of `dev-review`, `dev-security`, `dev-qa`, replace step 1 of the **Procedure** section with the same block as Task 10 step 3, substituting the corresponding skill name (`dev:review` / `dev:security` / `dev:qa`):

```markdown
1. Check the phase gate:
   ```
   python atelier/scripts/workflow.py <db_path> check-gate <project_id> <skill_name>
   ```
   Parse the JSON output: `{"allowed": bool, "current_phase": str, "required_phase": str, "reason": str}`.

   **If `allowed` is `true`**: record `current_phase` and proceed to the next step.

   **If `allowed` is `false`** (soft wall): ask the user:

   > *"Project is at `<current_phase>`. This skill normally requires `<required_phase>`. Proceed anyway? (yes / no)"*

   - On **yes**: run:
     ```
     python atelier/scripts/workflow.py <db_path> log-bypass <project_id> <skill_name> <current_phase> <required_phase>
     ```
     Optionally append `--agent <agent_id>` and `--note "<reason>"`. Then proceed to the next step.
   - On **no**: stop. Tell the user:
     > *"Advance to `<required_phase>` first (run `python atelier/scripts/workflow.py <db_path> advance <project_id> <required_phase>`), or pick a different skill."*
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_walled_skill_step1.py -v
```
Expected: PASS — all walled-skill tests pass (non-review + review skills).

- [ ] **Step 5: Commit**

```bash
git add skills/dev-review/SKILL.md skills/dev-security/SKILL.md \
        skills/dev-qa/SKILL.md tests/test_walled_skill_step1.py
git commit -m "refactor: walled review skills implement bypass flow"
```

---

## Task 12: Add bypass surfacing to `dev-handoff` retrospective

**Files:**
- Modify: `skills/dev-handoff/SKILL.md`
- Modify: `tests/test_handoff_with_bypasses.py` (new file)

When a project closes via `dev:handoff`, the retro summary must include a section listing all `phase_bypasses` rows for the project. This is the feedback loop that makes soft walls a useful audit trail rather than just silent permissiveness.

- [ ] **Step 1: Write the failing test**

Create `tests/test_handoff_with_bypasses.py`:

```python
"""Tests that dev-handoff's retro surfaces phase_bypasses for the project."""
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def test_handoff_skill_references_phase_bypasses_query():
    """dev-handoff's SKILL.md must include a step that queries phase_bypasses
    and surfaces the result in the retro summary."""
    path = SKILLS_DIR / "dev-handoff" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "phase_bypasses" in text, (
        "dev-handoff must query phase_bypasses to surface bypasses in retro"
    )
    # Must aggregate by skill so the retro is readable when there are many bypasses
    assert "GROUP BY skill" in text or "aggregat" in text.lower() or "by skill" in text.lower()


def test_handoff_describes_bypass_section_in_retro():
    path = SKILLS_DIR / "dev-handoff" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "Bypass" in text or "bypass" in text
    # The retro section format must accommodate a "Bypasses" subsection
    assert "retro" in text.lower() or "Retrospective" in text
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_handoff_with_bypasses.py -v
```
Expected: FAIL — `dev-handoff/SKILL.md` does not yet reference `phase_bypasses`.

- [ ] **Step 3: Add the bypass surfacing step to `dev-handoff/SKILL.md`**

In `skills/dev-handoff/SKILL.md`, find the existing retro-summary step (the step that produces the closing report for the project). Immediately before it, insert this new step:

````markdown
N. **Query phase bypasses for retro:**

   ```
   python -c "
   from scripts import db
   with db.connection('<db_path>') as conn:
       rows = conn.execute('''
           SELECT skill, current_phase, required_phase, COUNT(*) AS n, GROUP_CONCAT(note, ' | ') AS notes
           FROM phase_bypasses
           WHERE project_id = ?
           GROUP BY skill, current_phase, required_phase
           ORDER BY n DESC
       ''', (<project_id>,)).fetchall()
       for row in rows:
           print(row)
   "
   ```

   Format the output as a "Bypasses" subsection in the retro:

   - For each row: `<skill>: <n> bypass(es) from <current_phase> (normally requires <required_phase>)`. If `notes` is non-empty, append it.
   - If no rows, write: "*No phase bypasses during this project's lifecycle.*"
````

(Adjust step number `N.` to fit the existing numbered procedure.)

Then in the existing retro-summary step, add a sentence that pulls the bypass section into the retro output.

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_handoff_with_bypasses.py -v
```
Expected: PASS — both tests pass.

Run the full suite:

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add skills/dev-handoff/SKILL.md tests/test_handoff_with_bypasses.py
git commit -m "feat: dev-handoff retro surfaces phase_bypasses for the project"
```

---

## Task 13: Integration test — end-to-end bypass flow

**Files:**
- Create: `tests/test_skill_bypass_flow.py`

A single integration test that exercises the bypass pattern at the script level (since the SKILL.md procedures are agent-executed and can't be run in pytest directly). The test simulates what an agent does: call `check-gate`, get `allowed=false`, log a bypass, then call `check-gate` again at the correct phase and get `allowed=true`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_skill_bypass_flow.py`:

```python
"""End-to-end integration test for the soft-wall bypass flow.

Simulates what a dev skill's agent does at step 1:
  1. Call check-gate, observe allowed=false on wrong phase.
  2. (User confirms bypass) -- call log-bypass.
  3. Skill proceeds, eventually advances phase.
  4. Subsequent check-gate at correct phase returns allowed=true.
  5. Handoff retro query finds the bypass row.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import db, migrate, workflow
from scripts.projects import create_project

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_CLI = [sys.executable, str(REPO_ROOT / "scripts" / "workflow.py")]


@pytest.fixture
def project(tmp_path):
    db_path = tmp_path / "memex.db"
    migrate.run_migrations(str(db_path))
    pid = create_project(str(db_path), name="integration-test", created_by="agent-1")
    return str(db_path), pid


def _check_gate_cli(db_path: str, project_id: int, skill: str) -> dict:
    result = subprocess.run(
        WORKFLOW_CLI + [db_path, "check-gate", str(project_id), skill],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, f"check-gate failed: {result.stderr}"
    return json.loads(result.stdout)


def _log_bypass_cli(db_path: str, project_id: int, skill: str,
                    current: str, required: str, **kwargs) -> dict:
    args = WORKFLOW_CLI + [
        db_path, "log-bypass", str(project_id), skill, current, required,
    ]
    if "agent_id" in kwargs:
        args += ["--agent", kwargs["agent_id"]]
    if "note" in kwargs:
        args += ["--note", kwargs["note"]]
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"log-bypass failed: {result.stderr}"
    return json.loads(result.stdout)


def test_full_bypass_flow(project):
    """Project at design:open; user invokes dev:plan; bypass; advance; clean state."""
    db_path, pid = project

    # 1. Agent calls check-gate for dev:plan while project is at design:open
    result = _check_gate_cli(db_path, pid, "dev:plan")
    assert result["allowed"] is False
    assert result["current_phase"] == "design:open"
    assert result["required_phase"] == "design:approved"

    # 2. User confirms bypass; agent calls log-bypass
    bypass = _log_bypass_cli(
        db_path, pid, "dev:plan",
        result["current_phase"], result["required_phase"],
        agent_id="agent-1", note="user explicitly approved out-of-phase plan work",
    )
    assert "bypass_id" in bypass and bypass["bypass_id"] > 0

    # 3. Bypass is recorded in phase_bypasses
    with db.connection(db_path) as conn:
        row = conn.execute(
            "SELECT skill, current_phase, required_phase, agent_id, note "
            "FROM phase_bypasses WHERE id = ?", (bypass["bypass_id"],),
        ).fetchone()
    assert row == ("dev:plan", "design:open", "design:approved", "agent-1",
                   "user explicitly approved out-of-phase plan work")

    # 4. Agent later does explicit advancement (e.g., user actually wraps the design)
    workflow.advance_phase(db_path, pid, "design:approved")

    # 5. Subsequent check-gate for dev:plan now returns allowed=true
    result2 = _check_gate_cli(db_path, pid, "dev:plan")
    assert result2["allowed"] is True
    assert result2["current_phase"] == "design:approved"

    # 6. Bypass row remains (audit trail)
    with db.connection(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = ?", (pid,),
        ).fetchone()[0]
    assert count == 1


def test_bypass_aggregates_for_retro(project):
    """Multiple bypasses across the same (skill, current, required) within one
    minute deduplicate; across different keys they accumulate. Retro query
    must group correctly."""
    db_path, pid = project

    # Two bypasses of dev:plan from design:open within 60s -- dedup to one row
    _log_bypass_cli(db_path, pid, "dev:plan", "design:open", "design:approved")
    _log_bypass_cli(db_path, pid, "dev:plan", "design:open", "design:approved")

    # Advance to design:approved manually for test, then bypass dev:tdd
    workflow.advance_phase(db_path, pid, "design:approved")
    _log_bypass_cli(db_path, pid, "dev:tdd", "design:approved", "plan:approved")

    with db.connection(db_path) as conn:
        rows = conn.execute(
            """SELECT skill, current_phase, required_phase, COUNT(*) AS n
               FROM phase_bypasses
               WHERE project_id = ?
               GROUP BY skill, current_phase, required_phase
               ORDER BY skill""",
            (pid,),
        ).fetchall()
    # Expect two grouped rows: dev:plan (n=1 due to dedup) and dev:tdd-red (n=1)
    assert rows == [
        ("dev:plan", "design:open", "design:approved", 1),
        ("dev:tdd", "design:approved", "plan:approved", 1),
    ]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=. python -m pytest tests/test_skill_bypass_flow.py -v
```
Expected: PASS (because Tasks 1–3 already produced the underlying machinery). If FAIL: the test reveals an integration gap between `check-gate`, `log-bypass`, and `advance` — fix the underlying components rather than weakening the assertion.

If the test passes on first run, that's the desired outcome — Tasks 1–3 + 9–12 should have produced a working integration. The test is a regression guard, not a new feature gate.

- [ ] **Step 3: Run full suite**

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: ~214 tests pass total. Compare against the 189 baseline + ~25 new tests added across Tasks 1–13.

- [ ] **Step 4: Commit**

```bash
git add tests/test_skill_bypass_flow.py
git commit -m "test: add end-to-end integration test for soft-wall bypass flow"
```

---

## Task 14: Documentation — README, CLAUDE.md, CHANGELOG

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md` (atelier repo root)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update `README.md`**

After the existing "Setup" section, add a new section titled "Auto-trigger setup":

```markdown
### Auto-trigger setup

Atelier ships a SessionStart hook that injects its methodology into every Claude Code session, so the agent knows the trigger contract and the soft-wall bypass procedure from the first user message.

Add to your project's `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/atelier/hooks/session_start.py"
          }
        ]
      }
    ]
  }
}
```

As a fallback if the hook can't run, paste `templates/CLAUDE-snippet.md` into your project's `CLAUDE.md`.

### Soft walls

Phase gates are recommendations, not blocks. When a dev skill detects an out-of-phase invocation, it asks you to confirm a bypass, then logs the bypass to `phase_bypasses` for retrospective. Run `dev:handoff` at project close to see the bypass summary.
```

Also update the "Skills" table in the README to add `using-atelier` to the Session category:

```markdown
| Session | `using-atelier`, `ingest`, `save`, `load` |
```

- [ ] **Step 2: Update `CLAUDE.md` (atelier repo root)**

After the existing "Setup" section, add a new section explaining the canonical-source-plus-four-surfaces architecture:

```markdown
## Auto-trigger architecture

Atelier's methodology lives in a single canonical file (`skills/using-atelier/SKILL.md`) and is surfaced through four mechanisms:

1. **SessionStart hook** (`hooks/session_start.py`) — injects the canonical body as system context every session
2. **`session_open.py` extension** — appends phase-specific guidance after the existing phase announcement
3. **CLAUDE.md template snippet** (`templates/CLAUDE-snippet.md`) — short backup methodology for consumer projects
4. **YAML frontmatter** on four session-lifecycle skills (`using-atelier`, `ingest`, `save`, `load`)

When the methodology changes, edit only `skills/using-atelier/SKILL.md`. The hooks parse this file on every invocation, so changes propagate without redeployment. The CLAUDE.md snippet is the only mechanism that requires manual sync; keep it minimal.

## Soft walls

Phase gates (in `skill_gates` table) are advisory, not enforced. `workflow.py check_gate` returns a `GateResult` describing whether the current phase satisfies the gate; it never raises. Skills are responsible for the bypass-confirm-log flow when `allowed=False`. Bypasses are recorded in `phase_bypasses` and surfaced by `dev:handoff` retros.

Hard rule: **never reintroduce raising in `check_gate`.** If a downstream change makes the soft-wall flow feel insufficient, fix it at the policy layer (the `using-atelier` bypass procedure), not by re-walling the gate.
```

- [ ] **Step 3: Update `CHANGELOG.md`**

Add a new section at the top:

```markdown
## v0.2.0 — 2026-05-14

### Added
- `skills/using-atelier/SKILL.md` — canonical methodology source (trigger contract, Red Flags, phase guidance, dev arc, bypass procedure).
- `hooks/session_start.py` — SessionStart hook injecting `using-atelier` body as system context.
- `templates/CLAUDE-snippet.md` — backup methodology snippet for consumer projects' CLAUDE.md.
- `phase_bypasses` table and `workflow.py log-bypass` CLI subcommand for auditing soft-wall bypasses.
- YAML frontmatter with `description: Use when…` on `using-atelier`, `ingest`, `save`, `load`.
- `dev:handoff` retro now surfaces phase bypasses from `phase_bypasses` table.
- Migration `005_soft_walls.sql`.

### Changed
- `workflow.py check_gate` now returns a `GateResult` dataclass instead of raising `WorkflowError`. Out-of-phase invocations no longer block — skills ask the user to confirm a bypass.
- All dev skills' (`dev:design`, `dev:plan`, `dev:tdd`, `dev:review`, `dev:security`, `dev:qa`, `dev:diagnose`, `dev:handoff`) step 1 updated for the new JSON-based `check-gate` contract and (where walled) the bypass flow.
- `hooks/session_open.py` now appends phase-specific guidance derived from `using-atelier/SKILL.md`'s phase guidance table.

### Deprecated
- `WorkflowError` raise behavior in `check_gate`. Existing callers that try/except this exception should migrate to the `GateResult` API. (The exception class itself remains for `workflow.py advance` invalid-transition errors.)

### Migration notes
- Run `python scripts/migrate.py .ai/memex.db` to apply migration 005.
- Install the SessionStart hook per README "Auto-trigger setup" section.
- (Optional) paste `templates/CLAUDE-snippet.md` into your project's `CLAUDE.md`.
```

- [ ] **Step 4: Verify documentation renders**

```bash
cat README.md | head -100
cat CLAUDE.md
cat CHANGELOG.md | head -50
```
Expected: each file is well-formed markdown with the new sections in place.

- [ ] **Step 5: Run full test suite one last time**

```bash
PYTHONPATH=. python -m pytest -v
```
Expected: full suite passes. ~214 tests total.

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md CHANGELOG.md
git commit -m "docs: v0.2.0 -- auto-trigger setup, soft walls, bypass logging"
```

---

## Plan complete

Task count: 14 tasks across 6 layers (foundation, methodology, methodology surfaces, frontmatter, dev-skill procedures, verification & documentation). New code: ~1,200 lines. New tests: ~25. Modified SKILL.md files: 11 (plus 1 new: `using-atelier`).

Expected test count: 189 baseline + ~25 new = ~214 total, all passing.

**Order of execution**: tasks must run sequentially because Task 4 depends on Tasks 1–3 (the canonical file references `log-bypass`), Tasks 5–6 depend on Task 4 (hooks read the canonical file), and Tasks 9–12 depend on Tasks 1–3 (dev skills call the new CLI). Tasks 7 and 8 can run in parallel with Tasks 5–6 if desired. Task 13 must run last in the implementation layer. Task 14 is documentation and can run anytime after Task 13.

Final verification before declaring complete:
- All 14 commits land on the feature branch
- `python -m pytest -v` reports ~214 passing tests, 0 failing
- Manual scenarios from spec §5.2 are validated by hand
- No remaining TODO/FIXME/placeholder comments in any modified file






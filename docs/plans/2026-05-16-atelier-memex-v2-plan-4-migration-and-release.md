# Atelier ↔ Memex v2 Retrofit — Plan 4 of 4: Migration + Surface + Release (Waves 3, 4, P)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the retrofit. Implement the Local→Memex migration prompt, lock down skill surface invariants, refresh user docs, version-bump, and push.

**Architecture:** Wave 3 (migration) is mostly sequential — one procedure with crash safety + per-project markers. Wave 4 (surface + docs) parallelizes across 4 disjoint files. Wave P (release) is the final sequential cut.

**Tech Stack:** Python 3.10+, pytest, git, gh CLI.

**Spec reference:** [docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md](../specs/2026-05-16-atelier-memex-v2-retrofit-design.md) §§5, 8, 9, 12, 13.

---

## Parallel dispatch map

```
Wave 3 — Migration                              [serial; depends on Plan 3]
  T1:  migration replay function + per-project markers
  T2:  prompt UX + entry-skill integration (auto-prompt path)
  T2b: skills/migrate/SKILL.md (manual /atelier:migrate trigger, v1.1.0 surface)
  T3:  crash-safety + idempotency tests

Wave 4 — Surface + docs                         [parallel after Wave 3]
  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
  │ T4: surface     │ │ T5: CLAUDE.md   │ │ T6: README.md   │ │ T7: CHANGELOG   │
  │ invariant tests │ │ rewrite         │ │ rewrite         │ │ + version bump  │
  └─────────────────┘ └─────────────────┘ └─────────────────┘ └─────────────────┘

Wave P — Release                                [serial; final]
  T8: full test suite + lint
  T9: tag + push
  T10: update agora marketplace pin
```

---

### Task 1: Local→Memex migration replay (Wave 3)

**Files:**
- Create: `scripts/migrate_to_memex.py`
- Create: `internal/migrate-local-to-memex/SKILL.md`
- Test: `tests/test_migrate_to_memex.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_migrate_to_memex.py
"""End-to-end test: a project with a populated local atelier.db
migrates cleanly into a fake Memex install."""
import json
import shutil
import sqlite3
from pathlib import Path
import pytest
from scripts.migrate import apply_migrations

MIGRATIONS = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def populated_local_project(tmp_path, monkeypatch):
    """Create a project with .ai/atelier.db containing real rows."""
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    # Seed rows via the local backend (avoids mode detector clash)
    from scripts.mode_detector import _clear_cache
    _clear_cache()
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    from scripts.roles import create_role
    from scripts.agents import create_agent
    from scripts.projects import create_project
    from scripts.tasks import create_task
    from scripts.meetings import create_meeting
    r = create_role(str(db), name="Product Manager", description="PM")
    create_agent(str(db), id="atelier-pm-1", name="PM",
                 role_id=r["id"], profile="pm")
    create_project(str(db), name="myproj",
                   description="auth", created_by="atelier-pm-1")
    create_task(str(db), project_id=1, title="Fix bug",
                description="500 error", created_by="atelier-pm-1")
    create_meeting(str(db), root / ".ai" / "meetings",
                   title="Kickoff", date="2026-05-16",
                   summary="scope", decisions="oauth2",
                   created_by="atelier-pm-1")
    return root


def test_migration_replays_all_rows(populated_local_project, monkeypatch):
    """After migration, every local row appears in the Memex backend
    and the marker file is written."""
    # Mock memex-mode backend writes to capture what got replayed.
    captured = {"docs": [], "tasks": [], "meetings": [], "sessions": []}

    def fake_write_document(**kwargs):
        captured["docs"].append(kwargs)
        return {"status": "ingested", "index_id": "01a", "row_id": len(captured["docs"]),
                "key": kwargs["title"], "domain": kwargs["domain"], "relations": []}

    def fake_write_task(**kwargs):
        captured["tasks"].append(kwargs)
        return {"status": "ingested", "index_id": "01t", "row_id": len(captured["tasks"]),
                "key": kwargs["title"], "domain": "task", "relations": []}

    def fake_write_meeting(**kwargs):
        captured["meetings"].append(kwargs)
        return {"status": "ingested", "index_id": "01m", "row_id": len(captured["meetings"]),
                "key": kwargs["title"], "domain": "meeting", "relations": []}

    monkeypatch.setattr("scripts.backend_memex.write_document", fake_write_document)
    monkeypatch.setattr("scripts.backend_memex.write_task", fake_write_task)
    monkeypatch.setattr("scripts.backend_memex.write_meeting", fake_write_meeting)

    from scripts.migrate_to_memex import migrate_project
    summary = migrate_project(populated_local_project / ".ai" / "atelier.db")

    assert summary["migrated"]["projects"] == 1
    assert summary["migrated"]["tasks"] == 1
    assert summary["migrated"]["meetings"] == 1
    assert (populated_local_project / ".ai" / "atelier.migrated").exists()


def test_migration_renames_pre_migration_db(populated_local_project, monkeypatch):
    monkeypatch.setattr("scripts.backend_memex.write_document",
                        lambda **k: {"row_id": 1, "index_id": "x", "key": "k", "domain": "d", "relations": []})
    monkeypatch.setattr("scripts.backend_memex.write_task",
                        lambda **k: {"row_id": 1, "index_id": "x", "key": "k", "domain": "task", "relations": []})
    monkeypatch.setattr("scripts.backend_memex.write_meeting",
                        lambda **k: {"row_id": 1, "index_id": "x", "key": "k", "domain": "meeting", "relations": []})
    from scripts.migrate_to_memex import migrate_project
    migrate_project(populated_local_project / ".ai" / "atelier.db")
    assert not (populated_local_project / ".ai" / "atelier.db").exists()
    pre_migration_files = list((populated_local_project / ".ai").glob(
        "atelier-pre-migration-*.db"))
    assert len(pre_migration_files) == 1


def test_migration_failure_leaves_no_marker(populated_local_project, monkeypatch):
    """If any write fails, no .migrated marker is written and the local
    DB is NOT renamed."""
    def boom(**k):
        raise RuntimeError("simulated memex outage")
    monkeypatch.setattr("scripts.backend_memex.write_document", boom)
    from scripts.migrate_to_memex import migrate_project
    with pytest.raises(RuntimeError):
        migrate_project(populated_local_project / ".ai" / "atelier.db")
    assert (populated_local_project / ".ai" / "atelier.db").exists()
    assert not (populated_local_project / ".ai" / "atelier.migrated").exists()


def test_migration_skipped_when_marker_exists(populated_local_project, monkeypatch):
    """If the marker is already there, migrate_project returns 'skipped'."""
    marker = populated_local_project / ".ai" / "atelier.migrated"
    marker.write_text('{"migrated_at": "2026-01-01"}')
    from scripts.migrate_to_memex import migrate_project
    summary = migrate_project(populated_local_project / ".ai" / "atelier.db")
    assert summary["status"] == "skipped"


def test_decline_writes_local_only_marker(populated_local_project):
    """User declines migration → .local-only marker is written and
    subsequent commands won't re-prompt."""
    from scripts.migrate_to_memex import decline_migration
    decline_migration(populated_local_project / ".ai")
    assert (populated_local_project / ".ai" / "atelier.local-only").exists()
```

- [ ] **Step 2: Run tests — expect failure**

- [ ] **Step 3: Implement migration**

```python
# scripts/migrate_to_memex.py
"""Replay a project-local atelier.db into the machine-global Memex
substrate. Triggered once per project when Memex is detected.

Non-destructive on failure: no marker is written, the local DB is not
renamed, so the next Atelier command retries.
"""
from __future__ import annotations
import datetime
import json
import shutil
import sqlite3
from pathlib import Path


def _now_compact() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _connect_local(local_db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(local_db))
    c.row_factory = sqlite3.Row
    return c


def _index_id_for_atelier_row(source_ref: str) -> str | None:
    """Look up an atelier source_ref (e.g. `atelier:tasks:42`) in the
    Memex Index. Returns the existing `index_id` if already migrated,
    else None.

    The atelier replay layer uses this BEFORE calling
    `backend_memex.write_*` so a re-run after a partial outage skips
    rows that already landed in memex. Without this precheck, memex
    v2.3.0+ `librarian.write_entry` would raise `DuplicateKeyError` on
    every row from the prior partial run.
    """
    from scripts import backend_memex
    return backend_memex.lookup_index_id_by_source_ref(source_ref)


def migrate_project(local_db: Path) -> dict:
    """Replay local rows into Memex; on success rename the local DB
    and drop a .migrated marker.

    Idempotent: each row is checked against the Memex Index via its
    atelier source_ref (`atelier:<table>:<local_id>`) before writing.
    Rows already present are counted under `already_present` and
    skipped. This is what makes a post-outage re-run safe.

    Returns {"status": "migrated"|"skipped",
             "migrated": {table: count},
             "already_present": {table: count}}
    """
    ai_dir = local_db.parent
    marker = ai_dir / "atelier.migrated"
    if marker.exists():
        return {"status": "skipped", "migrated": {}, "reason": "marker present"}

    # Step 0: Refuse to migrate if Memex isn't bootstrapped.
    # memex v2.5.0+ raises MemexNotInitializedError from require_bootstrap()
    # if ~/.memex/registry.json is missing.
    from scripts import backend_memex
    from scripts import bootstrap
    try:
        from scripts import db as memex_db  # memex's db module (re-exported)
        memex_db.require_bootstrap()
    except Exception as e:
        # Surface MemexNotInitializedError (or any bootstrap failure) to the
        # caller with operator guidance. The entry-skill prompt logic catches
        # this and tells the user to run `memex:run` once before migrating.
        raise RuntimeError(
            f"Memex is not initialized: {e}. "
            f"Run `memex:run` once to bootstrap, then retry migration."
        ) from e

    # Run Atelier's idempotent bootstrap (seeds roles + agents into memex).
    bootstrap.run_bootstrap()

    c = _connect_local(local_db)
    migrated = {"projects": 0, "tasks": 0, "meetings": 0,
                "sessions": 0, "phase_bypasses": 0, "documents": 0}
    already_present = {"projects": 0, "tasks": 0, "meetings": 0,
                       "sessions": 0, "phase_bypasses": 0, "documents": 0}

    # Order matters: projects first so child rows reference real IDs.
    # Project rows
    for r in c.execute("SELECT * FROM projects"):
        source_ref = f"atelier:projects:{r['id']}"
        if _index_id_for_atelier_row(source_ref) is not None:
            already_present["projects"] += 1
            continue
        backend_memex.write_document(
            domain="project", title=r["name"],
            body=f"# {r['name']}\n\n{r['description'] or ''}",
            metadata={"name": r["name"], "description": r["description"],
                      "repo": r["repo"], "phase": r["phase"],
                      "local_id": r["id"], "source_ref": source_ref},
            caller_agent_id=r["created_by"],
        )
        migrated["projects"] += 1

    # Tasks
    for r in c.execute("SELECT * FROM tasks"):
        source_ref = f"atelier:tasks:{r['id']}"
        if _index_id_for_atelier_row(source_ref) is not None:
            already_present["tasks"] += 1
            continue
        backend_memex.write_task(
            title=r["title"], description=r["description"] or "",
            project_id=r["project_id"], created_by=r["created_by"],
            assigned_to=r["assigned_to"], priority=r["priority"] or 0,
            notes=r["notes"], source_ref=source_ref,
        )
        migrated["tasks"] += 1

    # Meetings
    for r in c.execute("SELECT * FROM meeting_minutes"):
        source_ref = f"atelier:meeting_minutes:{r['id']}"
        if _index_id_for_atelier_row(source_ref) is not None:
            already_present["meetings"] += 1
            continue
        backend_memex.write_meeting(
            title=r["title"], date=r["date"],
            summary=r["summary"] or "", decisions=r["decisions"] or "",
            created_by=r["created_by"], source_ref=source_ref,
        )
        migrated["meetings"] += 1

    # Sessions
    for r in c.execute("SELECT * FROM sessions"):
        backend_memex.upsert_session(
            project_id=r["project_id"], agent_id=r["agent_id"],
            phase=r["phase"], current_tasks=r["current_tasks"],
            accomplished=r["accomplished"], next_action=r["next_action"],
            status=r["status"], pm_notes=r["pm_notes"],
        )
        migrated["sessions"] += 1

    # Phase bypasses
    try:
        bypass_rows = list(c.execute("SELECT * FROM phase_bypasses"))
    except sqlite3.OperationalError:
        bypass_rows = []
    for r in bypass_rows:
        backend_memex.record_phase_bypass(
            project_id=r["project_id"], from_phase=r["from_phase"],
            to_phase=r["to_phase"], reason=r["reason"],
            agent_id=r["agent_id"],
        )
        migrated["phase_bypasses"] += 1

    # Project documents — go through write_document
    for r in c.execute("SELECT * FROM project_documents"):
        source_ref = f"atelier:project_documents:{r['id']}"
        if backend_memex.lookup_index_id_by_source_ref(source_ref):
            already_present["documents"] += 1
            continue
        # Migration replay uses a placeholder body — the historical file
        # at <workspace>/<filename> may already have been edited or
        # deleted since the original write. Indexing the placeholder is
        # acceptable here (migration semantics differ from create_document;
        # see spec §6.8 caveat). A future v1.1.1 pass could optionally
        # re-read disk files when present.
        body = f"# {r['title']}\n\nFile: {r['filename']}\n\nType: {r['type']}"
        backend_memex.write_document(
            domain=r["type"], title=r["title"], body=body,
            metadata={"project_id": r["project_id"],
                      "filename": r["filename"], "type": r["type"],
                      "source_ref": source_ref},
            caller_agent_id=r["created_by"],
        )
        migrated["documents"] += 1

    c.close()

    # All rows replayed successfully. Rename local DB + write marker.
    archive_name = f"atelier-pre-migration-{_now_compact()}.db"
    shutil.move(str(local_db), str(ai_dir / archive_name))
    marker.write_text(json.dumps({
        "migrated_at": _now_iso(), "migrated": migrated,
        "already_present": already_present,
        "archived_to": archive_name,
    }, indent=2), encoding="utf-8")
    return {"status": "migrated", "migrated": migrated,
            "already_present": already_present}


def decline_migration(ai_dir: Path) -> None:
    """User declined migration; record the choice so we don't re-prompt."""
    (ai_dir / "atelier.local-only").write_text(json.dumps({
        "declined_at": _now_iso(),
        "note": "Delete this file to re-enable the migration prompt.",
    }, indent=2), encoding="utf-8")


def should_prompt(ai_dir: Path) -> bool:
    """True if a project-local atelier.db exists and neither the
    migrated marker nor the local-only marker is present."""
    db = ai_dir / "atelier.db"
    if not db.exists():
        return False
    if (ai_dir / "atelier.migrated").exists():
        return False
    if (ai_dir / "atelier.local-only").exists():
        return False
    return True


def row_summary(local_db: Path) -> dict:
    """Quick row count per table for the migration-prompt message."""
    c = _connect_local(local_db)
    summary = {}
    for table in ("projects", "tasks", "meeting_minutes", "sessions",
                  "phase_bypasses", "project_documents"):
        try:
            row = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            summary[table] = row[0]
        except sqlite3.OperationalError:
            summary[table] = 0
    c.close()
    return summary
```

- [ ] **Step 4: Create `internal/migrate-local-to-memex/SKILL.md`**

```markdown
---
description: Internal — one-shot per-project migration from Local-mode atelier.db to machine-global Memex. Called only when mode_detector returns memex AND should_prompt returns True.
---

# migrate-local-to-memex (internal)

## Trigger
At the top of any Atelier user-facing skill in Memex mode, before any
real work: check `scripts.migrate_to_memex.should_prompt(<project>/.ai)`.

## Recipe

0. Verify Memex is bootstrapped. `migrate_project` internally calls
   `memex_db.require_bootstrap()` and raises a RuntimeError with operator
   guidance if `~/.memex/registry.json` is missing. If you see that
   error, instruct the user: "Run `memex:run` once before migrating",
   then abort the recipe.
1. Call `migrate_to_memex.row_summary(local_db)` to get a per-table count.
2. Present to the user:
   ```
   Memex v2 detected. Atelier currently has local data at .ai/atelier.db:
     - <N> projects
     - <N> tasks
     - <N> meeting minutes
     - <N> sessions

   Migrate to Memex now?  [y/N]
   ```
3. On y: call `migrate_to_memex.migrate_project(local_db)`. Report the
   returned summary to the user. Continue with the original command.
4. On N: call `migrate_to_memex.decline_migration(<project>/.ai)`. Continue
   in Local mode for this project.

## Re-entry semantics
- After successful migration the `atelier.migrated` marker prevents re-prompt.
- After decline, `atelier.local-only` marker prevents re-prompt.
- User can delete either marker to re-trigger.
```

- [ ] **Step 5: Run tests — expect pass**

```
pytest tests/test_migrate_to_memex.py -v
```

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_to_memex.py internal/migrate-local-to-memex/ tests/test_migrate_to_memex.py
git commit -m "feat(migrate): wave-3 Local-to-Memex migration with crash safety"
```

---

### Task 2: Prompt UX + entry-skill integration (Wave 3)

**Files:**
- Modify: `skills/load/SKILL.md`, `skills/save/SKILL.md`, `skills/ingest/SKILL.md`, `skills/run/SKILL.md`
- Create: `scripts/atelier_entrypoint.py` (shared startup-check function)
- Test: `tests/test_atelier_entrypoint.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_atelier_entrypoint.py
from pathlib import Path
from unittest.mock import patch
import pytest


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    (root / ".ai").mkdir()
    return root


def test_startup_in_local_mode_no_action(project_root, monkeypatch):
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    from scripts.atelier_entrypoint import startup_check
    r = startup_check()
    assert r["action"] == "proceed-local"


def test_startup_in_memex_mode_with_local_db_returns_prompt_action(
        project_root, monkeypatch):
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "memex")
    (project_root / ".ai" / "atelier.db").touch()
    from scripts.atelier_entrypoint import startup_check
    r = startup_check()
    assert r["action"] == "prompt-migration"
    assert "atelier.db" in r["local_db"]


def test_startup_in_memex_mode_no_local_db_proceeds(project_root, monkeypatch):
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "memex")
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap",
                        lambda: {"version": "1.1.0"})
    from scripts.atelier_entrypoint import startup_check
    r = startup_check()
    assert r["action"] == "proceed-memex"
```

- [ ] **Step 2: Run tests — expect failure**

- [ ] **Step 3: Implement `scripts/atelier_entrypoint.py`**

```python
# scripts/atelier_entrypoint.py
"""Shared startup check for Atelier user-facing skills.

Each of skills/{load,save,ingest,run}/SKILL.md calls this at the
top of its recipe. It returns an action token telling the skill what
to do before its actual work:

  - 'proceed-local'    — Memex absent; carry on with local backend
  - 'proceed-memex'    — Memex present + bootstrapped; carry on
  - 'prompt-migration' — Memex present + local DB exists + not yet
                         migrated/declined. The skill must surface the
                         migration prompt to the user before continuing.
"""
from __future__ import annotations
from pathlib import Path
from scripts import mode_detector
from scripts.migrate_to_memex import should_prompt, row_summary


def _project_ai_dir() -> Path | None:
    cur = Path.cwd().resolve()
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur / ".ai"
        cur = cur.parent
    return None


def startup_check() -> dict:
    mode = mode_detector.detect_mode()
    if mode == "local":
        return {"action": "proceed-local"}

    ai = _project_ai_dir()
    if ai is not None and should_prompt(ai):
        return {
            "action": "prompt-migration",
            "local_db": str(ai / "atelier.db"),
            "summary": row_summary(ai / "atelier.db"),
        }

    # Memex mode, no migration to do — ensure bootstrap is current.
    from scripts import bootstrap
    bootstrap_state = bootstrap.run_bootstrap()
    return {"action": "proceed-memex", "bootstrap": bootstrap_state}
```

- [ ] **Step 4: Update each of the 4 pre-existing user-facing SKILL.md files**

`skills/migrate/SKILL.md` (added in Task 2b) is intentionally excluded — it IS the manual migration path and would create circular pre-flight logic.

For each of `skills/{load,save,ingest,run}/SKILL.md`, add this block at the very top of its recipe section (BEFORE any existing instructions):

```markdown
## Pre-flight (always first)

Run `from scripts.atelier_entrypoint import startup_check; startup_check()`.

Branch on the returned `action`:

- **`proceed-local`** — Memex is not installed. Continue with the rest of
  this skill's recipe; all writes go to the project-local `.ai/atelier.db`.
- **`proceed-memex`** — Memex is installed and bootstrapped. Continue;
  all writes go through Memex.
- **`prompt-migration`** — Memex is installed but this project still
  has a local DB. Read `internal/migrate-local-to-memex/SKILL.md` and
  follow its prompt protocol. After the user answers, restart the
  pre-flight (`startup_check()` will now return `proceed-memex` or
  `proceed-local` depending on the user's choice).
```

- [ ] **Step 5: Run tests + manual sanity**

```
pytest tests/test_atelier_entrypoint.py -v
pytest tests/ -x
```

- [ ] **Step 6: Commit**

```bash
git add scripts/atelier_entrypoint.py skills/load/SKILL.md skills/save/SKILL.md skills/ingest/SKILL.md skills/run/SKILL.md tests/test_atelier_entrypoint.py
git commit -m "feat(entrypoint): wave-3 startup pre-flight + migration prompt in user-facing skills"
```

---

### Task 2b: Add `/atelier:migrate` user-facing skill (Wave 3)

Spec §9 + §15 #4 resolution: alongside the auto-prompt that fires when both Local DB and Memex are present (Task 2 above), atelier ships a dedicated `/atelier:migrate` skill so users can manually trigger the same migration. Use cases:

- User declined the auto-prompt (`.ai/atelier.local-only` marker exists) and now wants to migrate.
- A prior migration failed mid-flight; user wants to retry.
- User has many projects to migrate and wants to script the operation (one slash command per project).

**Files:**
- Create: `skills/migrate/SKILL.md`
- Test: `tests/test_skill_migrate_surface.py`

- [ ] **Step 1: Write the surface-invariant test**

```python
# tests/test_skill_migrate_surface.py
"""The /atelier:migrate skill is the 5th user-facing surface (v1.1.0).
This test pins its presence + frontmatter so accidental removal fails CI."""
from pathlib import Path
import re

REPO = Path(__file__).parent.parent


def test_migrate_skill_file_exists():
    assert (REPO / "skills" / "migrate" / "SKILL.md").is_file()


def test_migrate_skill_has_description_frontmatter():
    """Per CLAUDE.md, every public skill at skills/<name>/SKILL.md
    must carry YAML frontmatter with a `description` field."""
    md = (REPO / "skills" / "migrate" / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", md, re.DOTALL)
    assert m is not None, "skills/migrate/SKILL.md missing YAML frontmatter"
    fm = m.group(1)
    assert "description:" in fm, "frontmatter missing description field"
    # Description routing trigger should mention 'migrate' so /atelier:migrate
    # autocompletes for users typing 'migr…'
    assert "migrat" in fm.lower()


def test_migrate_skill_routes_to_internal_procedure():
    """The skill body should reference the internal procedure that does
    the actual work — same routing pattern as the other 4 skills."""
    md = (REPO / "skills" / "migrate" / "SKILL.md").read_text(encoding="utf-8")
    assert "internal/migrate-local-to-memex/SKILL.md" in md
```

- [ ] **Step 2: Run the test — expect FAIL (skill file missing).**

- [ ] **Step 3: Create `skills/migrate/SKILL.md`**

```markdown
---
description: Use when manually triggering or re-triggering Local → Memex migration for the current Atelier project — bypasses the .ai/atelier.local-only opt-out marker and retries on prior failure.
---

# migrate

Manual trigger for the Local → Memex migration documented in spec §8 and
implemented in `internal/migrate-local-to-memex/SKILL.md`. The same
migration logic auto-prompts at the top of `/atelier:{load,save,ingest,run}`
when both a project-local `.ai/atelier.db` and Memex are present and no
marker has been written. This skill is the **manual** path for the
exception cases.

## When to use

- You previously answered `n` to the auto-prompt and now want to migrate.
  (Auto-prompt won't fire again because `.ai/atelier.local-only` is set.)
- A prior migration failed partway through. You've resolved the cause
  (disk full, Memex bootstrap issue, etc.) and want to resume.
- You're scripting bulk migration across many projects: invoke
  `/atelier:migrate` from each project root rather than waiting for
  the auto-prompt at next session-open.

## Procedure

1. **Verify mode.** Run `from scripts.mode_detector import detect_mode;
   detect_mode()`. If it returns `"local"`, surface to user:
   "Memex is not installed (or not bootstrapped). Run `memex:run` once
   first, then re-invoke `/atelier:migrate`." Stop.

2. **Clear opt-out marker if present.** If `.ai/atelier.local-only`
   exists, delete it. (Whether the user explicitly opt-out is no longer
   relevant — they're explicitly opting back in now.)

3. **Verify there's something to migrate.** If `.ai/atelier.db` doesn't
   exist, surface: "No local atelier database in this project. Nothing
   to migrate." Stop.

4. **Run the internal migration procedure.** Read
   `internal/migrate-local-to-memex/SKILL.md` and follow it inline.
   The procedure is idempotent — rows already replayed (detected via
   `source_ref` lookup in memex's Index, per Plan 4 Task 1) are skipped
   and counted under `already_present`. Surface the summary to the user.

5. **On success**, the procedure writes `.ai/atelier.migrated` with the
   timestamp + row counts. On failure, the local DB is untouched and no
   marker is written; the user can fix the underlying issue and re-run
   this skill.

## Differences from the auto-prompt path

| Aspect | Auto-prompt (Task 2) | `/atelier:migrate` (this skill) |
|---|---|---|
| When triggered | Top of any other skill, on first session-open after Memex appears | User-invoked explicitly |
| Respects `.ai/atelier.local-only` | Yes — won't fire if marker present | **No** — clears the marker |
| Respects `.ai/atelier.migrated` | Yes — won't fire if marker present | **Same** — but the internal procedure detects already-migrated rows via source_ref and counts them rather than re-writing |
| Required answer | `y/N` from user | None — invocation IS the consent |

## Hard rules

- Never proceed if `detect_mode() != "memex"`. Atelier cannot migrate
  TO a target that doesn't exist.
- Never skip the internal procedure — call into
  `internal/migrate-local-to-memex/SKILL.md` directly so all replay
  logic, source_ref checks, and crash-safety guarantees are honored.
- Surface the row-count summary to the user before stopping. Do not
  swallow the migration's output.
```

- [ ] **Step 4: Run the test — expect PASS (3/3).**

- [ ] **Step 5: Commit**

```bash
git add skills/migrate/SKILL.md tests/test_skill_migrate_surface.py
git commit -m "feat(migrate): wave-3 dedicated /atelier:migrate skill for manual re-trigger"
```

---

### Task 3: Crash-safety + idempotency hardening (Wave 3)

**Files:**
- Modify: `scripts/migrate_to_memex.py` (add a transaction-savepoint wrapper)
- Test: `tests/test_migrate_crash_safety.py`

- [ ] **Step 1: Write a "crash on row 3" test**

```python
# tests/test_migrate_crash_safety.py
"""Verify migration is non-destructive on partial failure and that
re-running after a fix completes cleanly."""
from pathlib import Path
import pytest


@pytest.fixture
def project_with_data(tmp_path, monkeypatch):
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    from scripts.migrate import apply_migrations
    MIGRATIONS = Path(__file__).parent.parent / "migrations"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    from scripts.roles import create_role
    from scripts.agents import create_agent
    from scripts.projects import create_project
    from scripts.tasks import create_task
    r = create_role(str(db), name="PM", description="PM")
    create_agent(str(db), id="atelier-pm-1", name="PM",
                 role_id=r["id"], profile="x")
    for i in range(5):
        create_project(str(db), name=f"P{i}", description="d",
                       created_by="atelier-pm-1")
    for i in range(10):
        create_task(str(db), project_id=1, title=f"T{i}",
                    description="d", created_by="atelier-pm-1")
    return root


def test_failure_during_task_replay_leaves_no_marker(
        project_with_data, monkeypatch):
    """Inject failure on the 3rd task write. No marker is written, the
    local DB is not renamed, and a re-run succeeds when the issue clears."""
    fail_after = {"count": 0, "limit": 3}

    def flaky_write_task(**kwargs):
        fail_after["count"] += 1
        if fail_after["count"] > fail_after["limit"]:
            raise RuntimeError("simulated memex outage")
        return {"row_id": fail_after["count"], "index_id": "x",
                "key": "k", "domain": "task", "relations": []}

    monkeypatch.setattr("scripts.backend_memex.write_document",
                        lambda **k: {"row_id": 1, "index_id": "x",
                                     "key": "k", "domain": "d",
                                     "relations": []})
    monkeypatch.setattr("scripts.backend_memex.write_task", flaky_write_task)
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap",
                        lambda: {"version": "1.1.0"})

    from scripts.migrate_to_memex import migrate_project
    with pytest.raises(RuntimeError):
        migrate_project(project_with_data / ".ai" / "atelier.db")

    # Local DB intact
    assert (project_with_data / ".ai" / "atelier.db").exists()
    assert not (project_with_data / ".ai" / "atelier.migrated").exists()


def test_rerun_after_outage_is_idempotent(project_with_data, monkeypatch):
    """After the imaginary outage clears, re-running the migration
    succeeds. Idempotency is the responsibility of atelier's replay
    layer: before writing a row, `migrate_project` looks up the
    atelier source_ref (e.g., `atelier:tasks:42`) in the Memex Index
    via `_index_id_for_atelier_row()`. If found, the row is skipped
    and counted under `summary['already_present']`.

    Memex's `librarian.write_entry` raises `DuplicateKeyError` on key
    collision (v2.3.0+), so atelier must NOT rely on memex silently
    deduping — the precheck happens client-side.
    """
    # Simulate Index lookups that report "first 2 tasks already present
    # from the prior partial run". Remaining writes succeed normally.
    already_seen = {"atelier:tasks:1", "atelier:tasks:2"}

    def fake_index_lookup(source_ref: str) -> str | None:
        return "01t-prev" if source_ref in already_seen else None

    monkeypatch.setattr(
        "scripts.migrate_to_memex._index_id_for_atelier_row",
        fake_index_lookup,
    )
    monkeypatch.setattr("scripts.backend_memex.write_document",
                        lambda **k: {"row_id": 1, "index_id": "x",
                                     "key": "k", "domain": "d",
                                     "relations": []})
    monkeypatch.setattr("scripts.backend_memex.write_task",
                        lambda **k: {"row_id": 1, "index_id": "x",
                                     "key": "k", "domain": "task",
                                     "relations": []})
    monkeypatch.setattr("scripts.backend_memex.write_meeting",
                        lambda **k: {"row_id": 1, "index_id": "x",
                                     "key": "k", "domain": "meeting",
                                     "relations": []})
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap",
                        lambda: {"version": "1.1.0"})

    from scripts.migrate_to_memex import migrate_project
    summary = migrate_project(project_with_data / ".ai" / "atelier.db")
    assert summary["status"] == "migrated"
    # Two tasks should have been skipped as already-present
    assert summary.get("already_present", {}).get("tasks", 0) == 2
    assert (project_with_data / ".ai" / "atelier.migrated").exists()
```

- [ ] **Step 2: Run tests; if `migrate_project` already raises and leaves no marker (Task 1's implementation), green. Otherwise harden the implementation.**

Verify by reading `scripts/migrate_to_memex.py` — confirm the marker write and `shutil.move` happen ONLY after the entire row loop completes without exception. If any exception path was missed, fix.

- [ ] **Step 3: Commit (test-only)**

```bash
git add tests/test_migrate_crash_safety.py
git commit -m "test(migrate): wave-3 crash-safety regression coverage"
```

---

### Task 4: Skill-surface invariant tests (Wave 4 — parallel)

**Files:**
- Create: `tests/test_skill_surface.py`

- [ ] **Step 1: Write tests asserting exactly 5 user-facing skills + no internal SKILL.md leaks via plugin.json**

```python
# tests/test_skill_surface.py
"""Lock in the contract that Atelier exposes EXACTLY 5 user-facing skills
to Claude Code (v1.1.0 surface — adds `migrate` to the v1.0.13 set of 4)
and that every internal procedure stays under internal/."""
import json
from pathlib import Path

REPO = Path(__file__).parent.parent
SKILLS = REPO / "skills"
INTERNAL = REPO / "internal"


def test_exactly_five_user_skills():
    skill_dirs = [p for p in SKILLS.iterdir()
                  if p.is_dir() and (p / "SKILL.md").exists()]
    names = sorted(p.name for p in skill_dirs)
    assert names == ["ingest", "load", "migrate", "run", "save"], names


def test_plugin_manifest_lists_no_extra_skills():
    """If plugin.json declares any skill, it must be one of the five."""
    manifest_path = REPO / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        return  # nothing to check
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = data.get("skills", [])
    if isinstance(declared, list):
        for s in declared:
            name = s if isinstance(s, str) else s.get("name", "")
            assert any(name.endswith(n)
                       for n in ("load", "save", "ingest", "run", "migrate")), \
                f"manifest declares unknown skill: {name}"


def test_no_internal_skill_has_user_invocable_true():
    """Every internal SKILL.md must NOT have `user-invocable: true`."""
    for path in INTERNAL.rglob("SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert "user-invocable: true" not in text, \
            f"{path.relative_to(REPO)} declares user-invocable: true"


def test_internal_procedures_have_description_only_no_name_field():
    """Internal SKILL.md files must lack a top-level `name:` field that
    would register them as a slash command."""
    for path in INTERNAL.rglob("SKILL.md"):
        # Parse the first frontmatter block
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        try:
            _, frontmatter, _ = text.split("---", 2)
        except ValueError:
            continue
        for line in frontmatter.strip().splitlines():
            if line.startswith("name:"):
                # Some internal procedures have a name for documentation;
                # this is fine as long as they're not registered in plugin.json.
                # The plugin manifest test above is authoritative.
                pass
```

- [ ] **Step 2: Run tests; fix any surface violations**

```
pytest tests/test_skill_surface.py -v
```

If a leak is found (e.g., a fifth surfaced skill), move it under `internal/` and update its callers.

- [ ] **Step 3: Commit**

```bash
git add tests/test_skill_surface.py
git commit -m "test(surface): wave-4 lock the 4-visible-skills invariant"
```

---

### Task 5: Rewrite `CLAUDE.md` (Wave 4 — parallel)

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Read current CLAUDE.md to understand what's there**

```
cat CLAUDE.md
```

- [ ] **Step 2: Replace with v2-aware content**

```markdown
# CLAUDE.md — Atelier

Atelier is a shared workspace methodology for a human developer and a
multi-agent system working together on the same project. It runs in
either of two modes — automatically detected:

| Mode | When | Backend |
|---|---|---|
| **Memex** (preferred) | Memex v2 is installed in Claude Code | `~/.memex/atelier.db` registered as a Memex Core store; documents indexed in `~/.memex/index.db`; raw bodies archived to `~/.memex/raw/` |
| **Local** (fallback) | Memex is absent | `<project-root>/.ai/atelier.db` with FTS5-only retrieval. No federated index, no vector search. |

You never configure the mode — every Atelier command runs
`scripts.atelier_entrypoint.startup_check()` first and routes to the
right backend.

## Hard dependency
Atelier no longer requires Memex to be installed. If it's there,
Atelier uses it. If not, Atelier works locally.

## Setup

### Memex mode (zero ceremony)
On the first Atelier command in this mode, bootstrap runs automatically:
seeds Atelier's roles + agents into `~/.memex/agents.db`, creates the
`atelier` store via `memex:core:create-store`, writes
`~/.memex/atelier.bootstrap.json`. Idempotent. You don't run anything.

### Local mode (per project)
The first Atelier command in a repo creates `.ai/atelier.db` and applies
all migrations (shared + local-only). Add to `.git/info/exclude`:
```
.ai/
lessons/
```

## Migration

When Memex becomes available on a machine that has been running Atelier
locally, the next Atelier command in a project with `.ai/atelier.db`
will prompt:
```
Memex v2 detected. Migrate this project's Atelier data?  [y/N]
```
- **y** → migration replays every row through Memex; archives the local
  DB as `.ai/atelier-pre-migration-<ts>.db`; drops `.ai/atelier.migrated`.
- **N** → drops `.ai/atelier.local-only`. Atelier keeps using the local
  backend for this project even though Memex is available. Delete the
  marker to re-enable the prompt.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/backend.py` | Mode-dispatched persistence facade (every other module routes through here) |
| `scripts/backend_memex.py` | Memex-mode implementations |
| `scripts/backend_local.py` | Local-mode implementations |
| `scripts/mode_detector.py` | Detect + cache mode for the current process |
| `scripts/bootstrap.py` | Memex-mode bootstrap (idempotent) |
| `scripts/migrate_to_memex.py` | Per-project Local→Memex replay |
| `scripts/atelier_entrypoint.py` | `startup_check()` for user-facing skills |
| `scripts/migrate.py` | Apply SQL migrations to a SQLite file (used by Local mode + bootstrap) |
| `scripts/projects.py` etc. | Existing business modules — now thin wrappers around `backend.*` |

## Skills and procedures

| Location | Discoverable as `/atelier:<name>`? | Count |
|---|---|---|
| `skills/{ingest,load,migrate,run,save}/SKILL.md` | **Yes** | 5 |
| `internal/*` (procedures invoked via Read tool) | No | approximately 20 (see `internal/` directory) |

Internal procedures are reached only by reading the file from within a
user-facing skill — same pattern Memex v2 itself uses.

## Tests
```bash
pytest tests/
```
Most tests run in Local mode (no Memex install required in CI). The
Memex-mode tests use a fake-plugin fixture; the bootstrap e2e test is
skipped when the real Memex repo is not on disk.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): wave-4 rewrite for v2 dual-mode architecture"
```

---

### Task 6: Rewrite `README.md` (Wave 4 — parallel)

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Read current README**

```
cat README.md
```

- [ ] **Step 2: Replace user-facing intro + setup sections; preserve existing valuable content where applicable**

Focus changes:
- Remove the "requires Memex set up" prerequisite.
- Replace `.ai/memex.db` references with `.ai/atelier.db` (Local) or `~/.memex/atelier.db` (Memex mode).
- Document the auto-detection + migration prompt.
- Update the PYTHONPATH/setup instructions to reflect that no manual `scripts/migrate.py` invocation is required in either mode (Memex bootstrap and Local first-run handle it).
- Preserve workflow/methodology sections.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(README): wave-4 v2 dual-mode setup and migration"
```

---

### Task 7: CHANGELOG + version bump (Wave 4 — parallel)

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `.claude-plugin/plugin.json` (`1.0.13` → `1.1.0`)
- Modify: `pyproject.toml` if it exists (otherwise skip)
- Modify: any test pinning the version

- [ ] **Step 1: Append CHANGELOG entry**

```markdown
## v1.1.0 — 2026-05-16

**Memex v2 integration.** Atelier now writes through Memex v2 when
installed, with a slim project-local fallback otherwise.

**Memex compatibility:** Requires Memex **v2.2.0+** (API floor —
caller-built `librarian_output` landed in v2.2.0). Strongly recommended:
**v2.5.0+** (auto-bootstrap eliminates manual `python -m scripts.install`),
**v2.5.1+** (atelier can drop client-side `__*` namespace filtering).
Bootstrap refuses to run against Memex installs older than v2.2.0.

**Typed exceptions surfaced by memex.** Atelier callers may now see the
following typed exceptions propagated from memex:

- `librarian.DuplicateKeyError` — raised on key collision during
  `write_entry` (memex v2.3.0). Atelier's migration replay handles this
  via a client-side Index lookup before every write.
- `embeddings.EmbeddingUnavailable` — raised when embeddings can't be
  produced (oversized input, missing API key, provider error) (memex
  v2.4.1). Atelier surfaces the reason and falls back to FTS-only.
- `data_steward.OrphanNotFoundError` — raised when attempting to operate
  on an `index_id` that isn't present in the documents table (memex
  v2.4.0).
- `db.MemexNotInitializedError` — raised when `~/.memex/registry.json`
  is missing (memex v2.5.0). Atelier's `migrate_to_memex` catches and
  re-raises with operator guidance ("Run `memex:run` once before
  migrating").
- `db.MemexHomeInvalidError` — raised when `MEMEX_HOME` is set to an
  invalid path (memex v2.5.0).

### Added
- Dual-mode persistence facade (`scripts/backend.py`) — auto-selects
  between Memex Core and project-local SQLite.
- `scripts/backend_memex.py` — Tier 2 writes through
  `librarian.write_entry()` with caller-built `librarian_output` (no LLM
  dispatch for Atelier's structured domains); Tier 1 state mutations via
  Memex Core direct.
- `scripts/backend_local.py` — slim SQLite with FTS5 over a local
  `documents` table; raw bodies archived to `.ai/raw/`.
- `scripts/bootstrap.py` — idempotent Memex-mode bootstrap (seeds
  Atelier roles + shipped agents into `~/.memex/agents.db`; creates
  the `atelier` store; enforces Memex v2.2.0+ API floor; piggybacks
  on memex v2.5.0+ auto-bootstrap when available).
- `scripts/migrate_to_memex.py` — one-shot per-project replay from
  Local to Memex; crash-safe (no marker without full success).
- `scripts/atelier_entrypoint.py:startup_check()` — pre-flight for the
  four pre-existing user-facing skills (load, save, ingest, run); handles
  bootstrap + migration prompt. `/atelier:migrate` is excluded from
  pre-flight to avoid circular logic (it IS the migration path).
- `scripts/domain_vocabulary.py` — fixed Atelier domain set
  (`project` / `task` / `meeting` / `project_doc` / `adr`); validated
  on every Tier 2 write.
- `templates/roles.json` + `templates/agents/*.json` — Atelier-shipped
  role + agent seed data, used by both modes.
- `migrations/shared/` + `migrations/local-only/` — split so Memex mode
  consumes only schema-without-roles-or-agents (Memex's agents.db
  owns those tables). `migrations/shared/006_index_ids.sql` adds
  `index_id` columns required by `librarian.write_entry`.
- 8 new internal procedures under `internal/{memex,local,bootstrap-memex,
  migrate-local-to-memex}/` plus `internal/memex/domain-vocabulary.md`.

### Changed
- `scripts/{projects,tasks,documents,meetings,session,workflow,roles,
  agents}.py` rewired to call `backend.*` instead of opening SQLite
  directly. Public signatures unchanged.
- `CLAUDE.md` no longer requires Memex to be installed.

### Removed
- `scripts/db.py` — module's only consumer (the connection helper) is
  now inline in `scripts/migrate.py`.
- `.ai/memex.db` hard-dependency check.
```

- [ ] **Step 2: Bump `.claude-plugin/plugin.json`**

```diff
-  "version": "1.0.13",
+  "version": "1.1.0",
```

Also update the description if it mentions Memex v1.

- [ ] **Step 3: Verify any version-asserting tests or docs**

```
grep -rn "1\.0\.13" . --exclude-dir=.git
```
Check `CHANGELOG.md`, `.claude-plugin/plugin.json`, `pyproject.toml`,
`README.md`, and `tests/`. If anything pins `1.0.13`, bump to `1.1.0`.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md .claude-plugin/plugin.json tests/
git commit -m "release: bump to v1.1.0 (Memex v2 integration)"
```

---

### Task 8: Full test suite + lint (Wave P)

- [ ] **Step 1: Run everything**

```
pytest tests/ -v
```

Expected: all green.

- [ ] **Step 2: Quick smoke-test in both modes by hand**

Local-mode smoke:
```bash
cd /tmp && mkdir smoke-local && cd smoke-local && git init
PYTHONPATH=/home/nitekeeper/apps/atelier python -c "
from scripts.atelier_entrypoint import startup_check
print(startup_check())
"
```
Expected: `{'action': 'proceed-local'}`.

Memex-mode smoke (requires Memex installed):
```bash
cd /tmp/some-fresh-repo
PYTHONPATH=/home/nitekeeper/apps/atelier python -c "
from scripts.atelier_entrypoint import startup_check
print(startup_check())
"
```
Expected: `{'action': 'proceed-memex', ...}` and a fresh
`~/.memex/atelier.bootstrap.json` file.

- [ ] **Step 3: No commit (verification only)**

---

### Task 9: Tag + push (Wave P)

> **Note: release pipeline is intentionally manual.** Atelier does not
> have a `notify-agora.yml` GitHub Action equivalent to memex's
> auto-dispatch. After tagging here, Task 10 must be run by hand in the
> agora repo.

- [ ] **Step 1: Verify branch is clean**

```
git status
git log --oneline -20
```

- [ ] **Step 2: Push the branch**

```bash
git push -u origin feat/memex-v2-retrofit
```

- [ ] **Step 3: Confirm with user before tagging / merging to main**

This is a shared-state action. Pause and ask the user whether to:
1. Merge `feat/memex-v2-retrofit` → `main` and tag `v1.1.0` from main.
2. Open a PR for review first.

Wait for explicit user confirmation before continuing.

- [ ] **Step 4 (after user approval): Merge + tag + push**

```bash
git checkout main
git merge --ff-only feat/memex-v2-retrofit  # or 'git merge' for a merge commit
git tag -a v1.1.0 -m "Atelier v1.1.0 — Memex v2 integration"
git push origin main
git push origin v1.1.0
```

---

### Task 10: Update agora marketplace pin (Wave P, manual)

> This task is intentionally manual — no CI auto-dispatch exists.

**Files:**
- Modify (in agora repo, `/home/nitekeeper/apps/agora`):
  - `plugins.json`
  - `.claude-plugin/marketplace.json` (regenerated by the update script)

- [ ] **Step 1: From the agora repo, run the update**

```bash
cd /home/nitekeeper/apps/agora
python scripts/update.py atelier 2>&1 | tee /tmp/agora-update.out
```

Expected: stdout contains the substring `1.1.0`. The agora script's
output format is `<plugin>: <old> -> <new>` (verified against
`agora/scripts/update.py:147`), but assert via pattern match rather
than exact string to avoid fragility:

```bash
grep -q "1\.1\.0" /tmp/agora-update.out && echo OK || echo "no 1.1.0 in output"
```

- [ ] **Step 2: Diff to verify**

```
git diff plugins.json .claude-plugin/marketplace.json
```

- [ ] **Step 3: Commit + push**

```bash
git add plugins.json .claude-plugin/marketplace.json
git commit -m "update: bump atelier v1.0.13 -> v1.1.0 (Memex v2 retrofit)"
git push
```

---

## Plan 4 acceptance

- `scripts/migrate_to_memex.py` migrates every row through the right backend; crash-safe.
- `scripts/atelier_entrypoint.py:startup_check()` is called at the top of every pre-existing user-facing skill (load, save, ingest, run).
- 5 user-facing skills (`ingest`, `load`, `migrate`, `run`, `save`), ~20 internal procedures, no other surface drift.
- `CLAUDE.md` + `README.md` reflect v2 reality; no `.ai/memex.db` references remain.
- `CHANGELOG.md` has v1.1.0 entry.
- `pytest tests/` green.
- Agora marketplace pinned to v1.1.0.

## End-to-end acceptance (all four plans)

After all four plans land:

1. **Fresh user, no Memex** — `git init` a new repo, run any Atelier skill. Local mode kicks in, `.ai/atelier.db` is created, work proceeds. No `~/.memex/` written.
2. **Fresh user, Memex installed** — same flow. Memex bootstrap runs (one-shot, idempotent). All writes land in `~/.memex/atelier.db` + indexed in `~/.memex/index.db`. No project-local `.ai/atelier.db` is created.
3. **Existing local user, installs Memex later** — next Atelier command in the project surfaces the migration prompt; on consent every row replays through Memex and the local DB is archived.
4. **Existing local user, declines** — `.ai/atelier.local-only` written; Atelier continues in Local mode indefinitely. Deletes that marker to re-prompt.
5. **Memex uninstalled** — Local-mode falls back automatically. Data in `~/.memex/atelier.db` is not accessible until Memex is reinstalled; no migration back.

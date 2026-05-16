# Atelier ↔ Memex v2 Retrofit — Plan 3 of 4: Business-Logic Rewrites (Wave 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewire every Atelier script that opens SQLite to instead call `scripts.backend.*`. Delete the now-unused `scripts/db.py`. Existing tests continue to pass without modification because the facade selects the Local backend by default in test environments (no Memex install).

**Architecture:** Each script module gets the same treatment — its public functions remain identical in signature, but their bodies replace `sqlite3.connect(...)` patterns with `backend.<method>(...)`. The schemas are unchanged so the existing test surface is the regression detector.

**Tech Stack:** Python 3.10+, pytest. No new dependencies.

**Spec reference:** [docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md](../specs/2026-05-16-atelier-memex-v2-retrofit-design.md) §4.3 (facade), §6, §7.

---

## Parallel dispatch map

```
Wave 2 — 8 parallel rewrites + 1 cleanup, dispatched as one batch of 9
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ T1: documents.py │ │ T2: projects.py  │ │ T3: tasks.py     │ │ T4: meetings.py  │
└──────────────────┘ └──────────────────┘ └──────────────────┘ └──────────────────┘
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ T5: session.py   │ │ T6: workflow.py  │ │ T7: roles.py     │ │ T8: agents.py    │
└──────────────────┘ └──────────────────┘ └──────────────────┘ └──────────────────┘
                       ┌──────────────────────────────────────┐
                       │ T9: delete scripts/db.py + cleanup   │
                       └──────────────────────────────────────┘
```

All 9 tasks touch disjoint files in `scripts/` and their disjoint test files. Dispatch as one batch.

**Key invariant:** the public function signature on each script module is preserved. Existing tests in `tests/test_<module>.py` must continue to pass. The test fixtures (which still call `apply_migrations` against `migrations/shared` + `migrations/local-only` per Plan 1 Task 5 Step 9) pin Local mode behavior; Memex mode is exercised separately through Plan 2's tests.

---

### Task 1: Rewire `scripts/documents.py`

**Files:**
- Modify: `scripts/documents.py`
- Modify: `tests/test_documents.py` (only if existing fixtures changed; usually no change needed)

- [ ] **Step 1: Read existing module and tests to understand current public surface**

```
cat scripts/documents.py
cat tests/test_documents.py | head -50
```

Verify the exported functions: `create_document, get_document, update_document, delete_document, list_documents, search_documents`.

- [ ] **Step 2: Run existing tests to lock in current behavior**

```
pytest tests/test_documents.py -v
```
Expected: all green pre-rewrite.

- [ ] **Step 3: Rewrite the module**

```python
# scripts/documents.py
"""Project documents — wrapper around backend.write_document and
operational CRUD against the documents-pointer table.

Public surface unchanged from pre-retrofit. Internals now call the
mode-dispatched backend (Memex or Local)."""
from __future__ import annotations
from datetime import datetime, timezone
from scripts import backend


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_document(db_path: str, project_id: int, type: str,
                    title: str, filename: str, created_by: str) -> dict:
    """db_path parameter is retained for backwards compatibility with
    existing test fixtures; the backend determines storage location via
    mode detection. We pass body=filename so the indexed text contains
    the filename for FTS recall."""
    body = f"# {title}\n\nFile: {filename}\n\nProject document of type: {type}."
    result = backend.write_document(
        domain=type, title=title, body=body,
        metadata={"project_id": project_id, "filename": filename, "type": type},
        caller_agent_id=created_by,
    )
    return {
        "id": result["row_id"],
        "project_id": project_id,
        "type": type,
        "title": title,
        "filename": filename,
        "created_by": created_by,
        "created_at": _now(),
        "updated_at": _now(),
        "index_id": result.get("index_id"),
    }


def get_document(db_path: str, doc_id: int) -> dict | None:
    """In v2, document rows live in the backend. We don't have a dedicated
    project_documents table on the Memex side — instead, we treat docs as
    indexed documents and look them up by row_id via the backend."""
    # For Local mode, we read the project_documents table directly using
    # the existing pattern; for Memex mode, we look up via Core CRUD.
    from scripts import mode_detector
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        rows = backend_memex._memex_core_query(
            store="atelier", table="project_documents", where={"id": doc_id})
    else:
        from scripts import backend_local
        c = backend_local._conn()
        rows = c.execute("SELECT * FROM project_documents WHERE id = ?",
                         (doc_id,)).fetchall()
        rows = [dict(r) for r in rows]
        c.close()
    return rows[0] if rows else None


def update_document(db_path: str, doc_id: int, title: str | None = None,
                    filename: str | None = None) -> dict | None:
    changes = {}
    if title is not None:
        changes["title"] = title
    if filename is not None:
        changes["filename"] = filename
    changes["updated_at"] = _now()
    from scripts import mode_detector
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_update(
            store="atelier", table="project_documents",
            row_id=doc_id, changes=changes)
    from scripts import backend_local
    c = backend_local._conn()
    sets = ", ".join(f"{k} = ?" for k in changes)
    c.execute(f"UPDATE project_documents SET {sets} WHERE id = ?",
              tuple(changes.values()) + (doc_id,))
    c.commit()
    row = c.execute("SELECT * FROM project_documents WHERE id = ?",
                    (doc_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def delete_document(db_path: str, doc_id: int) -> bool:
    from scripts import mode_detector
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        memex_stores.query("atelier",
                           "DELETE FROM project_documents WHERE id = ?",
                           (doc_id,))
        return True
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute("DELETE FROM project_documents WHERE id = ?", (doc_id,))
    c.commit()
    deleted = cur.rowcount > 0
    c.close()
    return deleted


def list_documents(db_path: str, project_id: int) -> list[dict]:
    from scripts import mode_detector
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_query(
            store="atelier", table="project_documents",
            where={"project_id": project_id})
    from scripts import backend_local
    c = backend_local._conn()
    rows = c.execute("SELECT * FROM project_documents WHERE project_id = ?",
                     (project_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def search_documents(db_path: str, query: str,
                     project_id: int | None = None) -> list[dict]:
    return backend.find_documents(query=query, project_id=project_id)
```

- [ ] **Step 4: Run tests — expect green**

```
pytest tests/test_documents.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scripts/documents.py
git commit -m "refactor(documents): wave-2 route through backend facade"
```

---

### Task 2: Rewire `scripts/projects.py`

**Files:**
- Modify: `scripts/projects.py`

- [ ] **Step 1: Lock current behavior** — `pytest tests/test_projects.py -v` green.

- [ ] **Step 2: Rewrite**

```python
# scripts/projects.py
"""Projects — wrapper around backend writes for project-shaped rows."""
from __future__ import annotations
from datetime import datetime, timezone
from scripts import backend, mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_project(db_path: str, name: str, description: str | None,
                   created_by: str, repo: str | None = None) -> dict:
    body = f"# {name}\n\n{description or ''}\n\nRepo: {repo or 'n/a'}"
    result = backend.write_document(
        domain="project", title=name, body=body,
        metadata={"name": name, "description": description, "repo": repo},
        caller_agent_id=created_by,
    )
    return {
        "id": result["row_id"],
        "name": name,
        "description": description,
        "repo": repo,
        "phase": "design:in-progress",
        "created_by": created_by,
        "created_at": _now(),
        "updated_at": _now(),
        "index_id": result.get("index_id"),
    }


def get_project(db_path: str, project_id: int) -> dict | None:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        rows = backend_memex._memex_core_query(
            store="atelier", table="projects", where={"id": project_id})
    else:
        from scripts import backend_local
        c = backend_local._conn()
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)).fetchall()]
        c.close()
    return rows[0] if rows else None


def list_projects(db_path: str) -> list[dict]:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_query(store="atelier", table="projects")
    from scripts import backend_local
    c = backend_local._conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM projects").fetchall()]
    c.close()
    return rows


def update_project_phase(db_path: str, project_id: int, phase: str,
                         agent_id: str = "system") -> dict:
    return backend.transition_phase(
        project_id=project_id, to_phase=phase, agent_id=agent_id)


def update_project(db_path: str, project_id: int,
                   name: str | None = None,
                   description: str | None = None) -> dict | None:
    changes = {"updated_at": _now()}
    if name is not None: changes["name"] = name
    if description is not None: changes["description"] = description
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_update(
            store="atelier", table="projects",
            row_id=project_id, changes=changes)
    from scripts import backend_local
    c = backend_local._conn()
    sets = ", ".join(f"{k} = ?" for k in changes)
    c.execute(f"UPDATE projects SET {sets} WHERE id = ?",
              tuple(changes.values()) + (project_id,))
    c.commit()
    row = c.execute("SELECT * FROM projects WHERE id = ?",
                    (project_id,)).fetchone()
    c.close()
    return dict(row) if row else None
```

- [ ] **Step 3-4: Tests + commit**

```
pytest tests/test_projects.py -v
git add scripts/projects.py
git commit -m "refactor(projects): wave-2 route through backend facade"
```

---

### Task 3: Rewire `scripts/tasks.py`

**Files:**
- Modify: `scripts/tasks.py`

- [ ] **Step 1: Lock current behavior** — `pytest tests/test_tasks.py -v` green.

- [ ] **Step 2: Rewrite — preserve all public functions; route every state mutation through the backend**

```python
# scripts/tasks.py
"""Tasks — write every task through backend.write_task (which routes
through the Librarian in Memex mode). Status updates and assignment
go through backend.update_task_status / direct Core update."""
from __future__ import annotations
from datetime import datetime, timezone
from scripts import backend, mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_task(db_path: str, project_id: int, title: str,
                description: str | None, created_by: str,
                priority: int = 0, notes: str | None = None,
                assigned_to: str | None = None) -> dict:
    result = backend.write_task(
        title=title, description=description or "", project_id=project_id,
        created_by=created_by, assigned_to=assigned_to,
        priority=priority, notes=notes,
    )
    return {
        "id": result["row_id"], "project_id": project_id, "title": title,
        "description": description, "created_by": created_by,
        "assigned_to": assigned_to, "priority": priority, "notes": notes,
        "status": "pending",
        "created_at": _now(), "updated_at": _now(),
        "index_id": result.get("index_id"),
    }


def get_task(db_path: str, task_id: int) -> dict | None:
    return backend.get_task(task_id=task_id)


def list_tasks(db_path: str, project_id: int,
               status: str | None = None) -> list[dict]:
    return backend.list_tasks(project_id=project_id, status=status)


def assign_task(db_path: str, task_id: int, assigned_to: str) -> dict:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_update(
            store="atelier", table="tasks", row_id=task_id,
            changes={"assigned_to": assigned_to, "updated_at": _now()})
    from scripts import backend_local
    c = backend_local._conn()
    c.execute("UPDATE tasks SET assigned_to = ?, updated_at = ? WHERE id = ?",
              (assigned_to, _now(), task_id))
    c.commit()
    row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    c.close()
    return dict(row) if row else {}


def claim_task(db_path: str, task_id: int, agent_id: str) -> dict:
    return assign_task(db_path, task_id, agent_id)


def update_task_status(db_path: str, task_id: int, status: str,
                       notes: str | None = None) -> dict:
    return backend.update_task_status(
        task_id=task_id, status=status, notes=notes)


def complete_task(db_path: str, task_id: int) -> dict:
    return update_task_status(db_path, task_id, "complete")


def delete_task(db_path: str, task_id: int) -> bool:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        memex_stores.query("atelier", "DELETE FROM tasks WHERE id = ?",
                           (task_id,))
        return True
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    c.commit()
    deleted = cur.rowcount > 0
    c.close()
    return deleted
```

- [ ] **Step 3-4: Tests + commit**

```
pytest tests/test_tasks.py -v
git add scripts/tasks.py
git commit -m "refactor(tasks): wave-2 route through backend facade (Librarian on creates)"
```

---

### Task 4: Rewire `scripts/meetings.py`

**Files:**
- Modify: `scripts/meetings.py`

- [ ] **Step 1: Lock current behavior** — `pytest tests/test_meetings.py -v` green.

- [ ] **Step 2: Rewrite**

```python
# scripts/meetings.py
"""Meetings — use backend.write_meeting for inserts."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from scripts import backend, mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_meeting(db_path: str, meetings_dir: Path, title: str, date: str,
                   summary: str, decisions: str, created_by: str,
                   project_id: int | None = None) -> dict:
    """meetings_dir is honored only in Local mode (where it's
    <project-root>/.ai/meetings/). Memex mode routes through Archivist
    which writes to ~/.memex/raw/."""
    result = backend.write_meeting(
        title=title, date=date, summary=summary,
        decisions=decisions, created_by=created_by, project_id=project_id,
    )
    return {
        "id": result["row_id"], "title": title, "date": date,
        "summary": summary, "decisions": decisions,
        "created_by": created_by,
        "created_at": _now(), "updated_at": _now(),
        "index_id": result.get("index_id"),
    }


def get_meeting(db_path: str, meeting_id: int) -> dict | None:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        rows = backend_memex._memex_core_query(
            store="atelier", table="meeting_minutes",
            where={"id": meeting_id})
    else:
        from scripts import backend_local
        c = backend_local._conn()
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM meeting_minutes WHERE id = ?",
            (meeting_id,)).fetchall()]
        c.close()
    return rows[0] if rows else None


def list_meetings(db_path: str) -> list[dict]:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_query(
            store="atelier", table="meeting_minutes")
    from scripts import backend_local
    c = backend_local._conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM meeting_minutes").fetchall()]
    c.close()
    return rows


def add_participant(db_path: str, meeting_id: int, agent_id: str) -> dict:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_insert(
            store="atelier", table="meeting_participants",
            row={"meeting_id": meeting_id, "agent_id": agent_id})
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute(
        "INSERT INTO meeting_participants (meeting_id, agent_id) "
        "VALUES (?, ?) RETURNING *", (meeting_id, agent_id))
    row = cur.fetchone()
    c.commit()
    c.close()
    return dict(row) if row else {}
```

- [ ] **Step 3-4: Tests + commit**

```
pytest tests/test_meetings.py -v
git add scripts/meetings.py
git commit -m "refactor(meetings): wave-2 route through backend facade"
```

---

### Task 5: Rewire `scripts/session.py`

**Files:**
- Modify: `scripts/session.py`

- [ ] **Step 1: Lock** — `pytest tests/test_session.py -v` green.

- [ ] **Step 2: Rewrite the public surface**

```python
# scripts/session.py
"""Session state — wrapper around backend.upsert_session."""
from __future__ import annotations
from scripts import backend


def open_session(db_path: str, project_id: int, agent_id: str,
                 phase: str | None = None,
                 current_tasks: str | None = None) -> dict:
    return backend.upsert_session(
        project_id=project_id, agent_id=agent_id,
        phase=phase, current_tasks=current_tasks, status="in-progress",
    )


def close_session(db_path: str, project_id: int, agent_id: str,
                  accomplished: str, next_action: str,
                  pm_notes: str | None = None) -> dict:
    return backend.upsert_session(
        project_id=project_id, agent_id=agent_id,
        accomplished=accomplished, next_action=next_action,
        status="complete", pm_notes=pm_notes,
    )


def block_session(db_path: str, project_id: int, agent_id: str,
                  blocking_reason: str) -> dict:
    return backend.upsert_session(
        project_id=project_id, agent_id=agent_id,
        status="blocked", pm_notes=f"BLOCKED: {blocking_reason}",
    )


def get_current_session(db_path: str, project_id: int,
                        agent_id: str) -> dict | None:
    from scripts import mode_detector, backend_local, backend_memex
    if mode_detector.detect_mode() == "memex":
        rows = backend_memex._memex_core_query(
            store="atelier", table="sessions",
            where={"project_id": project_id, "agent_id": agent_id,
                   "status": "in-progress"})
        return rows[0] if rows else None
    c = backend_local._conn()
    row = c.execute(
        "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? "
        "AND status = 'in-progress' LIMIT 1",
        (project_id, agent_id)).fetchone()
    c.close()
    return dict(row) if row else None
```

- [ ] **Step 3-4: Tests + commit**

```
pytest tests/test_session.py -v
git add scripts/session.py
git commit -m "refactor(session): wave-2 route through backend facade"
```

---

### Task 6: Rewire `scripts/workflow.py`

**Files:**
- Modify: `scripts/workflow.py`

- [ ] **Step 1: Lock** — `pytest tests/test_workflow.py tests/test_phase_bypasses.py -v` green.

- [ ] **Step 2: Rewrite**

```python
# scripts/workflow.py
"""Phase-gate workflow — uses backend.transition_phase and
backend.record_phase_bypass. Phase catalog queries still use direct
SQL on the phases table (read-only static catalog)."""
from __future__ import annotations
from dataclasses import dataclass
from scripts import backend, mode_detector


@dataclass
class GateResult:
    allowed: bool
    current_phase: str
    required_phase: str | None
    skill: str | None
    message: str | None = None


def _phase_catalog_query(sql: str, params: tuple = ()) -> list[dict]:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        return memex_stores.query("atelier", sql, params)
    from scripts import backend_local
    c = backend_local._conn()
    rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    c.close()
    return rows


def check_gate(db_path: str, project_id: int, skill: str) -> GateResult:
    """Check whether a skill is allowed in the current project phase."""
    proj = _phase_catalog_query(
        "SELECT phase FROM projects WHERE id = ?", (project_id,))
    if not proj:
        return GateResult(allowed=False, current_phase="",
                          required_phase=None, skill=skill,
                          message=f"project {project_id} not found")
    current = proj[0]["phase"]
    gates = _phase_catalog_query(
        "SELECT required_phase FROM skill_gates WHERE skill = ?", (skill,))
    if not gates or gates[0]["required_phase"] is None:
        return GateResult(allowed=True, current_phase=current,
                          required_phase=None, skill=skill)
    required = gates[0]["required_phase"]
    allow_from_any = _phase_catalog_query(
        "SELECT allow_from_any FROM phases WHERE name = ?", (required,))
    if allow_from_any and allow_from_any[0]["allow_from_any"]:
        return GateResult(allowed=True, current_phase=current,
                          required_phase=required, skill=skill)
    allowed = (current == required)
    return GateResult(allowed=allowed, current_phase=current,
                      required_phase=required, skill=skill,
                      message=None if allowed else
                      f"skill {skill} requires phase {required}, currently {current}")


def transition(db_path: str, project_id: int, to_phase: str,
               agent_id: str) -> dict:
    """Validate the transition is in phase_transitions, then apply."""
    proj = _phase_catalog_query(
        "SELECT phase FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise ValueError(f"project {project_id} not found")
    from_phase = proj[0]["phase"]
    valid = _phase_catalog_query(
        "SELECT 1 FROM phase_transitions WHERE from_phase = ? AND to_phase = ?",
        (from_phase, to_phase))
    if not valid:
        raise ValueError(f"transition {from_phase} -> {to_phase} is not allowed")
    return backend.transition_phase(
        project_id=project_id, to_phase=to_phase, agent_id=agent_id)


def bypass_gate(db_path: str, project_id: int, from_phase: str,
                to_phase: str, reason: str, agent_id: str) -> dict:
    """Soft-wall override: log to phase_bypasses then transition."""
    backend.record_phase_bypass(
        project_id=project_id, from_phase=from_phase, to_phase=to_phase,
        reason=reason, agent_id=agent_id)
    return backend.transition_phase(
        project_id=project_id, to_phase=to_phase, agent_id=agent_id)
```

- [ ] **Step 3-4: Tests + commit**

```
pytest tests/test_workflow.py tests/test_phase_bypasses.py tests/test_soft_walls.py -v
git add scripts/workflow.py
git commit -m "refactor(workflow): wave-2 route through backend facade"
```

---

### Task 7: Rewire `scripts/roles.py`

**Files:**
- Modify: `scripts/roles.py`

The role functions still need to work in Local mode (where roles live in the local atelier.db) and Memex mode (where roles live in `~/.memex/agents.db`). The `db_path` parameter retained for back-compat in Local; ignored in Memex.

- [ ] **Step 1: Lock** — `pytest tests/test_roles.py -v` green.

- [ ] **Step 2: Rewrite**

```python
# scripts/roles.py
"""Roles — Local mode writes to local atelier.db; Memex mode forwards
to Memex's agents.db via Memex's roles module."""
from __future__ import annotations
from datetime import datetime, timezone
from scripts import mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _memex_roles_db() -> str:
    from pathlib import Path
    return str(Path.home() / ".memex" / "agents.db")


def create_role(db_path: str, name: str, description: str) -> dict:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import roles as memex_roles  # type: ignore
        return memex_roles.create_role(_memex_roles_db(), name=name,
                                       description=description)
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?) RETURNING *",
        (name, description, _now(), _now()))
    row = cur.fetchone()
    c.commit()
    c.close()
    return dict(row) if row else {}


def list_roles(db_path: str) -> list[dict]:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import roles as memex_roles  # type: ignore
        return memex_roles.list_roles(_memex_roles_db())
    from scripts import backend_local
    c = backend_local._conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM roles").fetchall()]
    c.close()
    return rows


def get_role(db_path: str, role_id: int) -> dict | None:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import roles as memex_roles  # type: ignore
        return memex_roles.get_role(_memex_roles_db(), role_id)
    from scripts import backend_local
    c = backend_local._conn()
    row = c.execute("SELECT * FROM roles WHERE id = ?",
                    (role_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def delete_role(db_path: str, role_id: int) -> bool:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import roles as memex_roles  # type: ignore
        return memex_roles.delete_role(_memex_roles_db(), role_id)
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute("DELETE FROM roles WHERE id = ?", (role_id,))
    c.commit()
    deleted = cur.rowcount > 0
    c.close()
    return deleted
```

- [ ] **Step 3-4: Tests + commit**

```
pytest tests/test_roles.py -v
git add scripts/roles.py
git commit -m "refactor(roles): wave-2 route through Memex agents.db when in Memex mode"
```

---

### Task 8: Rewire `scripts/agents.py`

**Files:**
- Modify: `scripts/agents.py`

Same shape as `roles.py` — mode-conditional forwarding.

- [ ] **Step 1: Lock** — `pytest tests/test_agents.py -v` green.

- [ ] **Step 2: Rewrite**

```python
# scripts/agents.py
"""Agents — Local mode writes to local atelier.db; Memex mode forwards
to Memex's agents.db via Memex's agents module."""
from __future__ import annotations
from datetime import datetime, timezone
from scripts import mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _memex_agents_db() -> str:
    from pathlib import Path
    return str(Path.home() / ".memex" / "agents.db")


def create_agent(db_path: str, id: str, name: str, role_id: int,
                 profile: str) -> dict:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import agents as memex_agents  # type: ignore
        return memex_agents.create_agent(_memex_agents_db(), id, name,
                                         role_id, profile)
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute(
        "INSERT INTO agents (id, name, role_id, profile, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
        (id, name, role_id, profile, _now(), _now()))
    row = cur.fetchone()
    c.commit()
    c.close()
    return dict(row) if row else {}


def get_agent(db_path: str, agent_id: str) -> dict | None:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import agents as memex_agents  # type: ignore
        return memex_agents.get_agent(_memex_agents_db(), agent_id)
    from scripts import backend_local
    c = backend_local._conn()
    row = c.execute("SELECT * FROM agents WHERE id = ?",
                    (agent_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def list_agents(db_path: str) -> list[dict]:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import agents as memex_agents  # type: ignore
        return memex_agents.list_agents(_memex_agents_db())
    from scripts import backend_local
    c = backend_local._conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM agents").fetchall()]
    c.close()
    return rows


def update_agent(db_path: str, agent_id: str, **fields) -> dict | None:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import agents as memex_agents  # type: ignore
        return memex_agents.update_agent(_memex_agents_db(),
                                         agent_id, **fields)
    from scripts import backend_local
    fields["updated_at"] = _now()
    c = backend_local._conn()
    sets = ", ".join(f"{k} = ?" for k in fields)
    c.execute(f"UPDATE agents SET {sets} WHERE id = ?",
              tuple(fields.values()) + (agent_id,))
    c.commit()
    row = c.execute("SELECT * FROM agents WHERE id = ?",
                    (agent_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def delete_agent(db_path: str, agent_id: str) -> bool:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import agents as memex_agents  # type: ignore
        return memex_agents.delete_agent(_memex_agents_db(), agent_id)
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    c.commit()
    deleted = cur.rowcount > 0
    c.close()
    return deleted
```

- [ ] **Step 3-4: Tests + commit**

```
pytest tests/test_agents.py -v
git add scripts/agents.py
git commit -m "refactor(agents): wave-2 route through Memex agents.db when in Memex mode"
```

---

### Task 9: Delete `scripts/db.py` + final cleanup

**Files:**
- Delete: `scripts/db.py`
- Modify: any remaining references (audit by `grep -r "from scripts.db" scripts/ tests/`)

- [ ] **Step 1: Find all remaining references**

```
grep -rn "scripts.db\|scripts/db\|from scripts import db" scripts/ tests/ migrations/
```

- [ ] **Step 2: For each reference, replace with the equivalent backend call OR `backend_local._conn()` if the module deliberately needs direct local SQLite (e.g., the `apply_migrations` runner in `scripts/migrate.py`).**

`scripts/migrate.py` is the one legitimate consumer of `db.py` — it opens connections to apply migrations. Move `get_connection` inline:

```python
# scripts/migrate.py — REPLACE the import
# Was: from scripts.db import get_connection
# Becomes:
import sqlite3
def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```

- [ ] **Step 3: Delete `scripts/db.py`**

```bash
git rm scripts/db.py
```

- [ ] **Step 4: Delete `tests/test_db.py` (the module it tests no longer exists)**

```bash
git rm tests/test_db.py
```

- [ ] **Step 5: Run full test suite**

```
pytest tests/ -x
```

Expected: ALL existing tests pass. New tests from Plans 1 & 2 also pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate.py
git commit -m "refactor(db): wave-2 delete scripts/db.py; migrate.py owns its own connection helper"
```

---

## Plan 3 acceptance

- `scripts/db.py` deleted; no remaining imports.
- `pytest tests/` green. Every pre-retrofit test still passes (Local mode is the default in fixtures).
- Every business module (documents, projects, tasks, meetings, session, workflow, roles, agents) routes through `backend.*` or mode-conditional helpers.
- Hand-off to Plan 4: migration procedure, surface invariants, release.

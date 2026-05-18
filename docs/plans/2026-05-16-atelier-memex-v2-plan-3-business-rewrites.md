# Atelier ↔ Memex v2 Retrofit — Plan 3 of 4: Business-Logic Rewrites (Wave 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewire every Atelier script that opens SQLite to instead call `scripts.backend.*`. Delete the now-unused `scripts/db.py`. Most existing tests pass without modification once Plan 1 Task 5's fixture updates land; a few module-specific tests need targeted updates (see per-task notes). The facade selects the Local backend by default in test environments (no Memex install).

**Architecture:** Each script module gets the same treatment — its public functions remain identical in signature, but their bodies replace `sqlite3.connect(...)` patterns with `backend.<method>(...)`. The schemas are unchanged so the existing test surface is the regression detector.

**Tech Stack:** Python 3.10+, pytest. No new dependencies.

**Spec reference:** [docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md](../specs/2026-05-16-atelier-memex-v2-retrofit-design.md) §4.3 (facade), §6, §7.

**`_memex_core_query` `where=` dict semantics (heads-up for all tasks below):** Plan 2's `backend_memex._memex_core_query(store=..., table=..., where={...})` helper supports only **`=` equality** matching on the keys/values it's given. For any non-equality filter (`IN`, `LIKE`, `IS NULL`, `<>`, range comparisons), callers must drop down to raw SQL via `memex_stores.query("atelier", "SELECT ... WHERE ...", params)` — `query()` is SELECT-only and never commits, so it's safe for reads. (For DELETE/UPDATE, use `memex_stores.delete(...)` / `memex_stores.update(...)`; see F1 and the per-task fixes for the canonical primitives.)

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
from pathlib import Path
from scripts import backend
from scripts.domain_vocabulary import TYPE_TO_DOMAIN
from scripts.workspace import workspace_root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_document(db_path: str, project_id: int, type: str,
                    title: str, filename: str, created_by: str,
                    workspace_id: int | None = None) -> dict:
    """Register a project document. Per spec §6.8, the indexed body MUST
    be the actual file content — placeholder bodies are explicitly
    forbidden because they make the doc undiscoverable, which is worse
    than a hard error at registration time.

    db_path is retained for backwards compatibility with existing test
    fixtures; the backend determines storage location via mode detection."""
    file_path = Path(workspace_root()) / filename
    if not file_path.exists():
        raise FileNotFoundError(
            f"Document file does not exist: {file_path}. "
            f"Create the markdown file first, then register with atelier."
        )
    body = file_path.read_text(encoding="utf-8")
    domain, subdomain = TYPE_TO_DOMAIN.get(type, ("project_doc", type))
    result = backend.write_document(
        workspace_id=workspace_id, project_id=project_id,
        domain=domain, subdomain=subdomain,
        title=title, body=body,
        metadata={"filename": filename, "type": type},
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
        # memex_stores.query() is SELECT-only and never commits. Use the
        # dedicated delete() primitive so the row is actually removed.
        memex_stores.delete(name="atelier", table="project_documents",
                            row_id=doc_id)
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

**Errors `create_document` may surface (Tier 2 write):**
- `FileNotFoundError` — caller-supplied `filename` doesn't exist under `workspace_root()`. Raised eagerly per spec §6.8 to avoid indexing a placeholder body.
- `scripts.agents.librarian.DuplicateKeyError` — document key collision. Surface to user as "A document with this key already exists. Most callers don't see this; if you do, the seq allocator may need investigation."
- `scripts.embeddings.EmbeddingUnavailable` — swallowed inside `backend_memex` per spec §6.2; no caller-side handling required here.

- [ ] **Step 4: Run tests — expect green**

Note: existing `tests/test_documents.py` likely passes filenames that don't exist on disk (the v1 implementation indexed a placeholder). Plan 1 Task 5 Step 9 fixture retrofit must create the on-disk files first, OR the tests need module-specific updates to set up a temp file before each `create_document` call. Flag this to the Plan 1 implementer if the fixture update is insufficient.

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
                   created_by: str, repo: str | None = None,
                   workspace_id: int | None = None,
                   slug: str | None = None) -> dict:
    """Projects route through backend.write_project — a distinct facade
    method (NOT write_document). Per spec §4.3, write_project takes
    (workspace_id, slug, name, description, created_by) as its
    canonical signature."""
    if slug is None:
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    result = backend.write_project(
        workspace_id=workspace_id, slug=slug, name=name,
        description=description, created_by=created_by,
    )
    return {
        "id": result["row_id"],
        "name": name,
        "slug": slug,
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
        # memex_stores.query() is SELECT-only and never commits. Use the
        # dedicated delete() primitive so the row is actually removed.
        memex_stores.delete(name="atelier", table="tasks", row_id=task_id)
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
"""Meetings — use backend.write_meeting for inserts, AND write the
canonical .ai/meetings/YYYY-MM-DD-<slug>.md file.

Per CLAUDE.md ("Meetings write two places"), the DB row and the
markdown file MUST stay in sync. The Memex Archivist also archives
the body to ~/.memex/raw/ on Tier 2 writes — that's a separate
content-addressable copy and is orthogonal to the workspace-local
.ai/meetings/ file that humans browse and grep."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from pathlib import Path
from scripts import backend, mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]


def _meeting_filename(date: str, title: str) -> str:
    return f"{date}-{_slugify(title)}.md"


def _render_meeting_md(title: str, date: str, summary: str,
                       decisions: str,
                       participants: list[str] | None = None) -> str:
    """Canonical markdown shape for .ai/meetings/*.md — must match the
    pre-retrofit format produced by scripts/meetings.py:_write_md so
    existing files and tests don't drift."""
    parts = [f"# {title}", "", f"Date: {date}", ""]
    if participants:
        parts.extend(["## Participants", ""] + [f"- {p}" for p in participants] + [""])
    parts.extend(["## Summary", "", summary, "", "## Decisions", "", decisions, ""])
    return "\n".join(parts)


def create_meeting(db_path: str, meetings_dir: Path, title: str, date: str,
                   summary: str, decisions: str, created_by: str,
                   project_id: int | None = None,
                   subdomain: str | None = None,
                   participants: list[str] | None = None,
                   workspace_id: int | None = None) -> dict:
    """Write both the markdown file (meetings_dir/<date>-<slug>.md) AND
    the backend row. Order: file first so a backend failure leaves an
    orphan file (recoverable by re-running create) rather than an
    orphan DB row pointing to nonexistent markdown.

    meetings_dir is always honored — it's the workspace-local
    .ai/meetings/ directory humans browse. Memex mode ALSO writes to
    ~/.memex/raw/ via Archivist, but that's a separate concern
    (content-addressable archive, not human-browsable workspace state)."""
    filename = _meeting_filename(date, title)
    file_path = Path(meetings_dir) / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        _render_meeting_md(title, date, summary, decisions, participants),
        encoding="utf-8",
    )
    result = backend.write_meeting(
        workspace_id=workspace_id, project_id=project_id,
        title=title, date=date, summary=summary, decisions=decisions,
        subdomain=subdomain, created_by=created_by,
    )
    return {
        "id": result["row_id"], "title": title, "date": date,
        "filename": filename,
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


def remove_participant(db_path: str, meeting_id: int, agent_id: str) -> None:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        # No row_id-based delete here (composite primary key). Use raw
        # SQL via the SELECT-only query() — but DELETE needs the
        # dedicated execute path. backend_memex provides a helper.
        backend_memex._memex_core_execute(
            store="atelier",
            sql="DELETE FROM meeting_participants WHERE meeting_id = ? AND agent_id = ?",
            params=(meeting_id, agent_id),
        )
        return
    from scripts import backend_local
    c = backend_local._conn()
    c.execute(
        "DELETE FROM meeting_participants WHERE meeting_id = ? AND agent_id = ?",
        (meeting_id, agent_id))
    c.commit()
    c.close()


def get_participants(db_path: str, meeting_id: int) -> list[dict]:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        # Cross-table JOIN — fall back to raw SQL via SELECT-only query().
        return memex_stores.query(
            "atelier",
            "SELECT a.* FROM agents a JOIN meeting_participants mp "
            "ON a.id = mp.agent_id WHERE mp.meeting_id = ?",
            (meeting_id,),
        )
    from scripts import backend_local
    c = backend_local._conn()
    rows = [dict(r) for r in c.execute(
        "SELECT a.* FROM agents a JOIN meeting_participants mp "
        "ON a.id = mp.agent_id WHERE mp.meeting_id = ?",
        (meeting_id,)).fetchall()]
    c.close()
    return rows


def update_meeting(db_path: str, meeting_id: int, **kwargs) -> dict | None:
    """Update meeting fields. Note: this does NOT rewrite the .ai/meetings/
    markdown file — content edits should go through a fresh create_meeting
    or a dedicated rewrite-markdown helper to keep file/DB in sync. The
    test harness covers this in test_meetings.py::test_update_meeting."""
    allowed = {"title", "date", "summary", "decisions"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    updates["updated_at"] = _now()
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_update(
            store="atelier", table="meeting_minutes",
            row_id=meeting_id, changes=updates)
    from scripts import backend_local
    c = backend_local._conn()
    sets = ", ".join(f"{k} = ?" for k in updates)
    c.execute(f"UPDATE meeting_minutes SET {sets} WHERE id = ?",
              tuple(updates.values()) + (meeting_id,))
    c.commit()
    row = c.execute("SELECT * FROM meeting_minutes WHERE id = ?",
                    (meeting_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def delete_meeting(db_path: str, meetings_dir: Path, meeting_id: int) -> bool:
    """Delete both the DB row(s) AND the .ai/meetings/*.md file. Order:
    fetch meeting first to learn the filename, then delete DB rows,
    then unlink the file. If the file is missing we tolerate that
    (idempotent cleanup)."""
    meeting = get_meeting(db_path, meeting_id)
    if meeting is None:
        return False
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        backend_memex._memex_core_execute(
            store="atelier",
            sql="DELETE FROM meeting_participants WHERE meeting_id = ?",
            params=(meeting_id,),
        )
        memex_stores.delete(name="atelier", table="meeting_minutes",
                            row_id=meeting_id)
    else:
        from scripts import backend_local
        c = backend_local._conn()
        c.execute("DELETE FROM meeting_participants WHERE meeting_id = ?",
                  (meeting_id,))
        c.execute("DELETE FROM meeting_minutes WHERE id = ?",
                  (meeting_id,))
        c.commit()
        c.close()
    md_file = Path(meetings_dir) / meeting["filename"]
    if md_file.exists():
        md_file.unlink()
    return True


def search_meetings(db_path: str, query: str) -> list[dict]:
    """Substring search across title/summary/decisions. Uses raw SQL
    because the LIKE pattern is non-equality (see preamble note on
    _memex_core_query where-dict semantics)."""
    pattern = f"%{query}%"
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        return memex_stores.query(
            "atelier",
            "SELECT * FROM meeting_minutes "
            "WHERE title LIKE ? OR summary LIKE ? OR decisions LIKE ? "
            "ORDER BY date DESC",
            (pattern, pattern, pattern),
        )
    from scripts import backend_local
    c = backend_local._conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM meeting_minutes "
        "WHERE title LIKE ? OR summary LIKE ? OR decisions LIKE ? "
        "ORDER BY date DESC",
        (pattern, pattern, pattern)).fetchall()]
    c.close()
    return rows
```

**Note on `_memex_core_execute`:** This helper does not exist in Plan 2's facade yet. Either (a) extend `backend_memex.py` to add a thin wrapper that opens an attached store connection and runs a raw SQL `execute()` with commit, OR (b) inline the equivalent (`memex_stores.delete(...)` for row-id deletes; for composite-key deletes the helper is required). Flag this dependency to the Plan 2 implementer if `_memex_core_execute` is not present; the alternative is to keep `meeting_participants` cleanup in a single `_memex_core_execute` shim added alongside this task.

**Errors `create_meeting` may surface (Tier 2 write):**
- `scripts.agents.librarian.DuplicateKeyError` — meeting key collision; surface to user as "A meeting with this key already exists. Most callers don't see this; if you do, the seq allocator may need investigation."
- `scripts.embeddings.EmbeddingUnavailable` — swallowed inside `backend_memex` per spec §6.2; no caller-side handling required here.

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
    """The status='in-progress' filter is equality, so _memex_core_query's
    where-dict works here. If we later add range or IN filters (e.g.,
    "all in-progress or blocked sessions"), drop to memex_stores.query()
    with raw SQL (see preamble note)."""
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

**Note on `upsert_session` column alignment:** `backend.upsert_session` routes to `backend_memex._memex_core_update` (when an existing row is found) or `_memex_core_insert` (when creating). Both should accept a `changes`/`row` dict keyed by column name, so they're robust against column-order drift. If the Plan 2 implementation uses raw SQL with positional placeholders instead, route updates through `memex_stores.update(name="atelier", table="sessions", row_id=<id>, updates={...})` for consistency. TODO for the implementer: verify by reading Plan 2 Task 4 — leave a brief code comment in `backend.upsert_session` if the underlying primitive is positional rather than keyword.

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
        # Memex roles module exposes delete; if not, fall back to
        # memex_stores.delete on the agents.db store.
        if hasattr(memex_roles, "delete_role"):
            return memex_roles.delete_role(_memex_roles_db(), role_id)
        from scripts import stores as memex_stores  # type: ignore
        memex_stores.delete(name="agents", table="roles", row_id=role_id)
        return True
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute("DELETE FROM roles WHERE id = ?", (role_id,))
    c.commit()
    deleted = cur.rowcount > 0
    c.close()
    return deleted


def update_role(db_path: str, role_id: int, **kwargs) -> dict | None:
    """Update role fields. In Memex mode, dispatches to memex_stores.update
    on the agents store (Memex's roles module does not currently expose
    an update_role helper). In Local mode, uses backend_local._conn().

    Preserves the pre-retrofit public signature: update_role(db_path,
    role_id, **kwargs). The db_path argument survives for signature
    compatibility; in Memex mode the path is resolved internally via
    _memex_roles_db()."""
    allowed = {"name", "description"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    updates["updated_at"] = _now()
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        memex_stores.update(name="agents", table="roles",
                            row_id=role_id, updates=updates)
        return get_role(db_path, role_id)
    from scripts import backend_local
    c = backend_local._conn()
    sets = ", ".join(f"{k} = ?" for k in updates)
    c.execute(f"UPDATE roles SET {sets} WHERE id = ?",
              tuple(updates.values()) + (role_id,))
    c.commit()
    row = c.execute("SELECT * FROM roles WHERE id = ?",
                    (role_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def search_roles(db_path: str, query: str) -> list[dict]:
    """Substring search on name + description. Uses raw SQL via the
    SELECT-only memex_stores.query() in Memex mode (LIKE is not
    expressible via the equality-only where-dict)."""
    pattern = f"%{query}%"
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        return memex_stores.query(
            "agents",
            "SELECT * FROM roles WHERE name LIKE ? OR description LIKE ? "
            "ORDER BY name",
            (pattern, pattern),
        )
    from scripts import backend_local
    c = backend_local._conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM roles WHERE name LIKE ? OR description LIKE ? "
        "ORDER BY name", (pattern, pattern)).fetchall()]
    c.close()
    return rows
```

**Note on Memex roles module surface:** verify against `/home/nitekeeper/apps/memex/scripts/roles.py` — it exports `create_role`, `get_role`, `list_roles`. Plan 3 forwards these directly; `update_role` / `delete_role` / `search_roles` are implemented here via `memex_stores.update`/`delete`/`query` because Memex doesn't expose dedicated helpers for them.

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


def list_agents(db_path: str, role_id: int | None = None) -> list[dict]:
    """List agents, optionally filtered by role_id (signature preserved
    from pre-retrofit). When role_id is None, returns all agents."""
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import agents as memex_agents  # type: ignore
        all_agents = memex_agents.list_agents(_memex_agents_db())
        if role_id is None:
            return all_agents
        return [a for a in all_agents if a.get("role_id") == role_id]
    from scripts import backend_local
    c = backend_local._conn()
    if role_id is not None:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM agents WHERE role_id = ? ORDER BY name",
            (role_id,)).fetchall()]
    else:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM agents ORDER BY name").fetchall()]
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
        if hasattr(memex_agents, "delete_agent"):
            return memex_agents.delete_agent(_memex_agents_db(), agent_id)
        from scripts import stores as memex_stores  # type: ignore
        memex_stores.delete(name="agents", table="agents", row_id=agent_id)
        return True
    from scripts import backend_local
    c = backend_local._conn()
    cur = c.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    c.commit()
    deleted = cur.rowcount > 0
    c.close()
    return deleted


def search_agents(db_path: str, query: str,
                  role_id: int | None = None) -> list[dict]:
    """Substring search on name + profile, optionally filtered by
    role_id. Signature preserved from pre-retrofit. In Memex mode
    falls back to raw SQL via memex_stores.query() (LIKE not
    expressible via equality-only where-dict)."""
    pattern = f"%{query}%"
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        if role_id is not None:
            return memex_stores.query(
                "agents",
                "SELECT * FROM agents WHERE role_id = ? "
                "AND (name LIKE ? OR profile LIKE ?) ORDER BY name",
                (role_id, pattern, pattern),
            )
        return memex_stores.query(
            "agents",
            "SELECT * FROM agents WHERE name LIKE ? OR profile LIKE ? "
            "ORDER BY name",
            (pattern, pattern),
        )
    from scripts import backend_local
    c = backend_local._conn()
    if role_id is not None:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM agents WHERE role_id = ? "
            "AND (name LIKE ? OR profile LIKE ?) ORDER BY name",
            (role_id, pattern, pattern)).fetchall()]
    else:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM agents WHERE name LIKE ? OR profile LIKE ? "
            "ORDER BY name", (pattern, pattern)).fetchall()]
    c.close()
    return rows
```

**Note on Memex agents module surface:** verify against `/home/nitekeeper/apps/memex/scripts/agents/__init__.py` — it exports `create_agent`, `get_agent`, `list_agents`. `update_agent` / `delete_agent` / `search_agents` are implemented locally via `memex_stores.update`/`delete`/`query` (Memex doesn't expose dedicated helpers).

**Persona preservation:** All ~50 existing agent personas (per user decision) MUST survive the Local→Memex migration. The Plan 4 replay step is responsible for round-tripping each agent row through `create_agent` against the Memex agents.db; Plan 3's `agents.py` rewrite only needs to keep reads/writes functional. The canonical PM role name is "Product Manager" — when seeding/migrating, normalize legacy variants ("PM", "product-manager", etc.) to this canonical form.

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
- Modify: `scripts/migrate.py` (inline its own connection helper)
- Modify: `scripts/seed_roles.py` (currently `from scripts.db import get_connection` at line 12 — rewire through the facade or `backend_local._conn()`)
- Modify: any other remaining references (audit by `grep -r "from scripts.db" scripts/ tests/`)

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

- [ ] **Step 2b: Rewire `scripts/seed_roles.py`**

`seed_roles.py` imports `from scripts.db import get_connection` at line 12 and uses it to bulk-insert role rows. Route through the facade so it works in both modes:

```python
# scripts/seed_roles.py — REPLACE the import
# Was: from scripts.db import get_connection
# Becomes:
from scripts import roles  # the wave-2-rewritten module that mode-dispatches
```

Then replace each direct INSERT with `roles.create_role(db_path, name=..., description=...)`. If the seed needs idempotency (re-running shouldn't error on existing roles), either (a) check existence via `roles.search_roles(db_path, name)` first, or (b) add a `backend.find_or_create_role(name, description)` helper to the facade and route through that. Recommend (b) — it's a one-line addition to `scripts/backend.py` that dispatches to `find_or_create_role` in `backend_local` / `backend_memex`. **Flag to Plan 2 implementer if `find_or_create_role` is not present in the facade.**

When normalizing role names during seed, the canonical PM role name is "Product Manager" (per user decision). Legacy variants like "PM", "product-manager", "Project Manager" should be rewritten to "Product Manager" at seed time.

Alternative: if Plan 2 ships `scripts/bootstrap.py` that fully subsumes role/agent seeding, document that decision here and add `git rm scripts/seed_roles.py` to Plan 4 instead of rewiring it. Pick one path and don't leave both partially done.

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
git add scripts/migrate.py scripts/seed_roles.py
git commit -m "refactor(db): wave-2 delete scripts/db.py; migrate.py owns its own connection helper; seed_roles routes through facade"
```

---

## Plan 3 acceptance

- `scripts/db.py` deleted; no remaining imports.
- `pytest tests/` green. Every pre-retrofit test still passes (Local mode is the default in fixtures).
- Every business module (documents, projects, tasks, meetings, session, workflow, roles, agents) routes through `backend.*` or mode-conditional helpers.
- Hand-off to Plan 4: migration procedure, surface invariants, release.

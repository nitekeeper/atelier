# scripts/tasks.py
"""Tasks — route every state mutation through the backend facade.

Writes go through `backend.write_task` (Librarian-mediated in Memex mode,
direct INSERT in Local mode). Reads use `backend.get_task` / `list_tasks`.
Status / claim / complete flow goes through `backend.update_task_status`
so the timestamp side-effects (claimed_at / completed_at) live in one
place.

assign_task is the one mutation NOT covered by the backend surface (Plan 2
intentionally scoped `update_task_status` to status-only writes); we route
it through the active backend's `_conn()` helper so the connection still
honours the mode-dispatched DB path.

## Priority TEXT → INT coercion

v1.0.13 stored `tasks.priority` as TEXT with a CHECK constraint over
'critical'|'high'|'medium'|'low'. v1.1.0 declares the column INTEGER
DEFAULT 0 (migrations/shared/001_v110_schema.sql:86) so ORDER BY priority
is a fast numeric compare instead of a fragile CASE expression. SQLite
will silently accept TEXT into an INTEGER column thanks to type affinity,
which makes the footgun easy to miss — so we coerce at the seam.

The map is intentionally a single source of truth (`_PRIORITY_MAP`) so
the same lookup also serves Plan 4's legacy reader once it lands.

Unknown strings collapse to 0 ("no priority") rather than raising — the
priority surface is advisory; a typo shouldn't crash a task create.
"""
from __future__ import annotations
from datetime import datetime, timezone

from scripts import backend, mode_detector


_PRIORITY_MAP = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _coerce_priority(p) -> int:
    """Coerce a priority input (TEXT or INT) to the v1.1.0 INTEGER form.

    - String: case-folded lookup in `_PRIORITY_MAP`. Unknown → 0.
    - None: 0 (column default).
    - Anything else: int() coercion (lets callers pass float / numpy ints
      without ceremony).
    """
    if p is None:
        return 0
    if isinstance(p, str):
        return _PRIORITY_MAP.get(p.lower(), 0)
    return int(p)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_conn():
    """Open a connection against the active backend's DB.

    Used by the two writes that don't have a backend surface yet
    (assign_task, delete_task). Imports lazily so the Memex code path
    is never reached when running in Local mode.
    """
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex  # caller must use the memex-specific helpers
    from scripts import backend_local
    return backend_local._conn()


# ── Writes ─────────────────────────────────────────────────────────────────

def create_task(db_path: str, project_id: int, title: str,
                created_by: str, description: str | None = None,
                priority=0, notes: str | None = None,
                assigned_to: str | None = None,
                workspace_id: int = 1,
                subdomain: str | None = None) -> dict:
    """Create a task. `priority` accepts INT (preferred) or the legacy
    TEXT form ('critical'|'high'|'medium'|'low'); both are coerced to
    the v1.1.0 INTEGER column before the write.

    `db_path` is preserved for signature parity with v1.0.x callers; the
    backend facade resolves the active DB itself (Memex Index or
    workspace-rooted `.ai/atelier.db`).

    `workspace_id` defaults to 1 (the singleton workspace; spec §10
    multi-workspace lands in v1.2). Callers that already know the
    workspace can override.
    """
    result = backend.write_task(
        workspace_id=workspace_id, project_id=project_id,
        title=title, description=description or "",
        subdomain=subdomain, created_by=created_by,
        assigned_to=assigned_to,
        priority=_coerce_priority(priority),
        notes=notes,
    )
    # backend.write_task returns the full row in Local mode; in Memex mode
    # it returns `{"row_id": ..., "index_id": ...}`. Re-fetch via get_task
    # so callers see a consistent shape regardless of mode.
    task = get_task(db_path, result["row_id"])
    if task is None:
        # Memex-mode writes may return a different row id semantics; fall
        # back to synthesising from the write result + inputs.
        return {
            "id": result["row_id"], "project_id": project_id, "title": title,
            "description": description, "created_by": created_by,
            "assigned_to": assigned_to,
            "priority": _coerce_priority(priority),
            "notes": notes, "status": "pending",
            "created_at": _now(), "updated_at": _now(),
            "index_id": result.get("index_id"),
        }
    return task


def update_task(db_path: str, task_id: int, **kwargs) -> dict:
    """Partial update. Routes status changes through
    `backend.update_task_status` so claimed_at / completed_at stay
    coherent; routes everything else through the active backend's
    `_conn()` so the write still respects mode dispatch.
    """
    allowed = {"title", "description", "priority", "notes", "status", "assigned_to"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_task(db_path, task_id)

    if "priority" in updates:
        updates["priority"] = _coerce_priority(updates["priority"])

    # Status-only or status+notes goes through the facade so the timestamp
    # side-effects land via the canonical path.
    if set(updates.keys()) <= {"status", "notes"} and "status" in updates:
        return backend.update_task_status(
            task_id=task_id, status=updates["status"],
            notes=updates.get("notes"),
        )

    # General path: write directly via the active backend's connection.
    # Memex mode isn't covered here — the deferred update_task surface
    # remains a backend.write/_memex_core_update follow-up (Plan 3 Task 3
    # body explicitly defers assign + delete + general update to the
    # mode-aware shortcut).
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        changes = dict(updates)
        changes["updated_at"] = _now()
        backend_memex._memex_core_update(
            store="atelier", table="tasks", row_id=task_id, changes=changes,
        )
        return get_task(db_path, task_id)

    from scripts import backend_local
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    c = backend_local._conn()
    try:
        c.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?",
            (*updates.values(), task_id),
        )
        c.commit()
    finally:
        c.close()
    return get_task(db_path, task_id)


def delete_task(db_path: str, task_id: int) -> bool:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        memex_stores.delete(name="atelier", table="tasks", row_id=task_id)
        return True
    from scripts import backend_local
    c = backend_local._conn()
    try:
        cur = c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def assign_task(db_path: str, task_id: int, agent_id: str) -> dict:
    """Set `assigned_to` AND flip status → 'assigned'.

    Two-field update isn't on the backend surface (Plan 2 scoped
    `update_task_status` to status-only); we write the columns directly
    via the active backend's connection and let the v1.1.0 row factory
    surface a clean dict.
    """
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        return backend_memex._memex_core_update(
            store="atelier", table="tasks", row_id=task_id,
            changes={"assigned_to": agent_id, "status": "assigned",
                     "updated_at": _now()},
        )
    from scripts import backend_local
    c = backend_local._conn()
    try:
        c.execute(
            "UPDATE tasks SET assigned_to = ?, status = 'assigned', "
            "updated_at = ? WHERE id = ?",
            (agent_id, _now(), task_id),
        )
        c.commit()
        row = c.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        c.close()
    return dict(row) if row else {}


def claim_task(db_path: str, task_id: int, agent_id: str) -> dict:
    """Agent claims a task previously assigned to them.

    Pre-condition: task must exist AND `assigned_to == agent_id`.
    On success, status flips to 'in-progress' (claimed_at side-effect
    lands via `backend.update_task_status`).
    """
    task = get_task(db_path, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    if task["assigned_to"] != agent_id:
        raise ValueError(f"Task {task_id} is not assigned to {agent_id}")
    return update_task_status(db_path, task_id, status="in-progress")


def update_task_status(db_path: str, task_id: int, status: str,
                       notes: str | None = None) -> dict:
    return backend.update_task_status(
        task_id=task_id, status=status, notes=notes,
    )


def complete_task(db_path: str, task_id: int) -> dict:
    return update_task_status(db_path, task_id, status="complete")


# ── Reads ──────────────────────────────────────────────────────────────────

def get_task(db_path: str, task_id: int) -> dict | None:
    return backend.get_task(task_id=task_id)


def list_tasks(db_path: str, status: str | None = None,
               assigned_to: str | None = None,
               project_id: int | None = None) -> list[dict]:
    """List tasks, optionally filtered.

    `backend.list_tasks` requires `project_id` (spec §4.3) so the
    backend can stay efficient (no full-table scan). When the caller
    omits `project_id`, we fall back to a direct connection on the
    active backend.
    """
    if project_id is not None:
        rows = backend.list_tasks(project_id=project_id, status=status)
        if assigned_to is not None:
            rows = [r for r in rows if r.get("assigned_to") == assigned_to]
        return rows

    # No project filter — backend surface doesn't cover this; reach into
    # the local connection. Memex mode would require a cross-project
    # search; defer to v1.2 (callers should always pass project_id today).
    if mode_detector.detect_mode() == "memex":
        raise NotImplementedError(
            "list_tasks without project_id is not supported in Memex mode "
            "(spec §4.3 list_tasks requires project_id; cross-project "
            "search lands in v1.2)."
        )
    from scripts import backend_local
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if assigned_to:
        conditions.append("assigned_to = ?")
        params.append(assigned_to)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    c = backend_local._conn()
    try:
        rows = c.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at",
            params,
        ).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def search_tasks(db_path: str, query: str,
                 status: str | None = None,
                 assigned_to: str | None = None) -> list[dict]:
    """LIKE-search across title / description / notes.

    No backend surface yet (FTS5 covers `project_documents` only — Plan 2
    Task 7 deliberately scoped FTS to the document table). Reach into
    the active backend's connection so the search still respects mode
    dispatch in Local mode.
    """
    if mode_detector.detect_mode() == "memex":
        raise NotImplementedError(
            "search_tasks is not supported in Memex mode yet "
            "(Memex-side task FTS lands with the v1.2 task domain)."
        )
    from scripts import backend_local
    pattern = f"%{query}%"
    conditions = ["(title LIKE ? OR description LIKE ? OR notes LIKE ?)"]
    params = [pattern, pattern, pattern]
    if status:
        conditions.append("status = ?")
        params.append(status)
    if assigned_to:
        conditions.append("assigned_to = ?")
        params.append(assigned_to)
    where = "WHERE " + " AND ".join(conditions)
    c = backend_local._conn()
    try:
        rows = c.execute(
            f"SELECT * FROM tasks {where} "
            f"ORDER BY priority DESC, created_at",
            params,
        ).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys
    import json
    import argparse

    db_path = ".ai/memex.db"
    cmd = sys.argv[1]

    if cmd == "create":
        parser = argparse.ArgumentParser()
        parser.add_argument("project_id", type=int)
        parser.add_argument("title")
        parser.add_argument("created_by")
        parser.add_argument("--description")
        parser.add_argument("--priority", default=0)
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(create_task(db_path, project_id=args.project_id, title=args.title,
                                      created_by=args.created_by, description=args.description,
                                      priority=args.priority), indent=2))
    elif cmd == "get":
        result = get_task(db_path, int(sys.argv[2]))
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "assign":
        print(json.dumps(assign_task(db_path, int(sys.argv[2]), sys.argv[3]), indent=2))
    elif cmd == "claim":
        print(json.dumps(claim_task(db_path, int(sys.argv[2]), sys.argv[3]), indent=2))
    elif cmd == "complete":
        print(json.dumps(complete_task(db_path, int(sys.argv[2])), indent=2))
    elif cmd == "update":
        parser = argparse.ArgumentParser()
        parser.add_argument("task_id", type=int)
        parser.add_argument("--notes")
        parser.add_argument("--title")
        parser.add_argument("--description")
        parser.add_argument("--priority")
        args = parser.parse_args(sys.argv[2:])
        kwargs = {k: v for k, v in vars(args).items() if k != "task_id" and v is not None}
        print(json.dumps(update_task(db_path, args.task_id, **kwargs), indent=2))
    elif cmd == "list":
        parser = argparse.ArgumentParser()
        parser.add_argument("--status")
        parser.add_argument("--assigned_to")
        parser.add_argument("--project_id", type=int)
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(list_tasks(db_path, status=args.status,
                                     assigned_to=args.assigned_to,
                                     project_id=args.project_id), indent=2))
    elif cmd == "search":
        parser = argparse.ArgumentParser()
        parser.add_argument("query")
        parser.add_argument("--status")
        parser.add_argument("--assigned_to")
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(search_tasks(db_path, args.query,
                                       status=args.status,
                                       assigned_to=args.assigned_to), indent=2))

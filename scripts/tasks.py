# scripts/tasks.py
"""Tasks — route every state mutation through the backend facade.

Writes go through `backend.write_task` (Librarian-mediated in Memex mode,
direct INSERT in Local mode). Reads use `backend.get_task` / `list_tasks`.
Status / claim / complete flow goes through `backend.update_task_status`
so the Local-mode timestamp side-effects (claimed_at / completed_at) live
in one place.

# Note: backend_memex.update_task_status does not set claimed_at/completed_at
# yet — Plan 4 v1.2 followup.

assign_task / delete_task / general update_task all ride the backend
facade now (`backend.assign_task`, `backend.delete_task`,
`backend.update_task`) — Plan 2's status-only `update_task_status` was
widened in v1.2 with dedicated facade methods so this module no longer
reaches into `_memex_core_update` / `backend_local._conn()` directly.

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

import logging
from datetime import datetime, timezone

from scripts import backend, mode_detector

_log = logging.getLogger(__name__)

_PRIORITY_MAP = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _coerce_priority(p) -> int:
    """Coerce a priority input (TEXT or INT) to the v1.1.0 INTEGER form.

    - String: case-folded lookup in `_PRIORITY_MAP`. Unknown → 0.
      Stringified ints ("0".."4") are accepted via int() fallback so
      legacy CLI muscle memory keeps working.
    - None: 0 (column default).
    - bool: int(True) == 1, int(False) == 0 — booleans are ints in Python,
      so `_coerce_priority(True)` returns 1 by design. Don't pass bools.
    - Anything else: int() coercion (lets callers pass float / numpy ints
      without ceremony).
    """
    if p is None:
        return 0
    if isinstance(p, str):
        key = p.lower()
        if key in _PRIORITY_MAP:
            return _PRIORITY_MAP[key]
        # Accept stringified ints from the CLI ("0".."4"); fall through
        # to 0 on anything else (priority is advisory — typo ≠ crash).
        try:
            return int(p)
        except ValueError:
            return 0
    return int(p)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Writes ─────────────────────────────────────────────────────────────────


def create_task(
    db_path: str,
    project_id: int,
    title: str,
    created_by: str,
    description: str | None = None,
    priority=0,
    notes: str | None = None,
    assigned_to: str | None = None,
    workspace_id: int = 1,
    subdomain: str | None = None,
    parallel_group: int | None = None,
    team_pk: str | None = None,
) -> dict:
    """Create a task. `priority` accepts INT (preferred) or the legacy
    TEXT form ('critical'|'high'|'medium'|'low'); both are coerced to
    the v1.1.0 INTEGER column before the write.

    `db_path` is preserved for signature parity with v1.0.x callers; the
    backend facade resolves the active DB itself (Memex Index or
    workspace-rooted `.ai/atelier.db`).

    `workspace_id` defaults to 1 (the singleton workspace; spec §10
    multi-workspace lands in v1.2). Callers that already know the
    workspace can override.

    `parallel_group` is an optional operator-meaningful integer tag
    (atelier#34 — reintroduced via migration 004). NULL by default;
    consumed by atelier#39's planner+dispatch wave grouping.

    `team_pk` is an optional run/cycle correlation id (atelier#90 —
    migration 010). NULL by default; consumed by `scripts/status.py`'s
    per-cycle scoping when one project hosts >1 concurrent team/cycle.
    """
    result = backend.write_task(
        workspace_id=workspace_id,
        project_id=project_id,
        title=title,
        description=description or "",
        subdomain=subdomain,
        created_by=created_by,
        assigned_to=assigned_to,
        priority=_coerce_priority(priority),
        notes=notes,
        parallel_group=parallel_group,
        team_pk=team_pk,
    )
    # backend.write_task returns the full row in Local mode; in Memex mode
    # it returns `{"row_id": ..., "index_id": ...}`. Re-fetch via get_task
    # so callers see a consistent shape regardless of mode.
    task = get_task(db_path, result["row_id"])
    if task is None:
        # Memex-mode writes may return a different row id semantics; fall
        # back to synthesising from the write result + inputs. Log a
        # warning — synthesis means the round-trip read missed; callers
        # see a row that wasn't actually re-fetched.
        _log.warning(
            "create_task: re-fetch missed for row_id=%s; synthesising row "
            "from write inputs (Memex-mode id semantics)",
            result["row_id"],
        )
        now = _now()
        return {
            "id": result["row_id"],
            "project_id": project_id,
            "title": title,
            "description": description,
            "created_by": created_by,
            "assigned_to": assigned_to,
            "priority": _coerce_priority(priority),
            "notes": notes,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "index_id": result.get("index_id"),
        }
    return task


def update_task(db_path: str, task_id: int, **kwargs) -> dict:
    """Partial update routed through the backend facade.

    `backend.update_task` does NOT accept `status` — the facade rejects
    it with a dedicated ValueError because status writes must preserve
    the lifecycle-timestamp side-effects (claimed_at / completed_at)
    that live in `update_task_status`. To keep this CLI seam tolerant,
    when callers mix `status` with other columns we split the work:
    first the status flip through `update_task_status` (capturing the
    timestamp side-effects), then any remaining columns through
    `backend.update_task`. The final returned row is the post-second-call
    state so callers see all updates reflected.

    The facade also rejects unknown columns with `ValueError`, so we
    filter callers' kwargs to the allowed set here as a friendlier
    "ignore the junk" path. This module is the legacy CLI entry point;
    ignoring unknown kwargs preserves that surface's tolerance.
    """
    allowed = {
        "title",
        "description",
        "priority",
        "notes",
        "status",
        "assigned_to",
        "parallel_group",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_task(db_path, task_id)

    if "priority" in updates:
        updates["priority"] = _coerce_priority(updates["priority"])

    # Split status from the rest. backend.update_task rejects `status` to
    # protect the COALESCE timestamps in update_task_status; route it through
    # the canonical path here.
    status_val = updates.pop("status", None)
    notes_val = updates.get("notes")

    if status_val is not None:
        # Apply the status flip (+notes if present) first so the lifecycle
        # timestamps land via the canonical path. update_task_status takes
        # `notes` directly, so pull it from `updates` if we also have other
        # columns to write below — we don't want the same notes write to
        # fire twice.
        result = backend.update_task_status(
            task_id=task_id,
            status=status_val,
            notes=notes_val,
        )
        # If status was the only thing (plus optional notes), we're done.
        remaining = (
            {k: v for k, v in updates.items() if k != "notes"} if notes_val else dict(updates)
        )
        if not remaining:
            return result
        # Otherwise, drop notes from `updates` (already applied) and fall
        # through to the regular partial-update path for the rest.
        if notes_val is not None:
            updates.pop("notes", None)

    if not updates:
        # Nothing left after handling status / notes — return current row.
        return get_task(db_path, task_id)

    return backend.update_task(task_id=task_id, **updates)


def delete_task(db_path: str, task_id: int) -> bool:
    return backend.delete_task(task_id=task_id)


def assign_task(db_path: str, task_id: int, agent_id: str) -> dict:
    """Set `assigned_to` AND flip status → 'assigned' atomically via
    the backend facade. The two-field update is a single statement
    downstream — see `backend.assign_task`."""
    return backend.assign_task(task_id=task_id, agent_id=agent_id)


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


def update_task_status(db_path: str, task_id: int, status: str, notes: str | None = None) -> dict:
    return backend.update_task_status(
        task_id=task_id,
        status=status,
        notes=notes,
    )


def complete_task(db_path: str, task_id: int) -> dict:
    return update_task_status(db_path, task_id, status="complete")


# ── PM dispatch-state mutators (atelier#60 / migration 006) ──────────────────
#
# Thin wrappers the wave-5 dispatch loop calls so it never hand-writes SQL.
# These mutate the state-machine columns added by migration 006
# (attempts / last_attempt_at / abandon_category / abandoned_ack_at). The
# wave-scheduler ENGINE stays mode-agnostic (atelier#61 owns mode dispatch);
# these helpers carry the mode routing.
#
# Local mode is fully implemented via the dedicated `backend_local` mutators.
# Memex-mode parity is a documented followup — mirroring the existing gap
# noted at the top of this module (`backend_memex.update_task_status` does not
# yet set claimed_at/completed_at). The columns live in `migrations/shared/`
# so they exist in both backends' SQLite; only the Memex-mode write path is
# pending. Until then these raise a clear `NotImplementedError` in Memex mode
# rather than silently writing to the wrong store via `backend_local`.


def _dispatch_state_memex_guard(fn_name: str) -> None:
    if mode_detector.detect_mode() == "memex":
        raise NotImplementedError(
            f"tasks.{fn_name}: PM dispatch-state columns (atelier#60 / migration "
            "006) are Local-mode only for now; Memex-mode parity is a followup "
            "(mirrors the backend_memex.update_task_status gap)."
        )


def increment_attempt(db_path: str, task_id: int) -> dict:
    """Bump `tasks.attempts` by one (the 5-attempt budget). A wall-clock
    soft-kill counts as an attempt, so the wave loop calls this once per
    dispatch attempt."""
    _dispatch_state_memex_guard("increment_attempt")
    from scripts import backend_local

    return backend_local.increment_attempt(task_id=task_id)


def stamp_last_attempt(db_path: str, task_id: int) -> dict:
    """Stamp `tasks.last_attempt_at` with the current UTC time so the
    scheduler can compute stall age against the 30-min wall-clock cap."""
    _dispatch_state_memex_guard("stamp_last_attempt")
    from scripts import backend_local

    return backend_local.stamp_last_attempt(task_id=task_id)


def set_abandoned(db_path: str, task_id: int, category: str) -> dict:
    """Mark a task abandoned (status -> 'abandoned' + abandon_category).
    `category` is the parsed TM-006 abandon-grammar token; the task becomes
    wave-terminal immediately (the ack stamp is audit-only)."""
    _dispatch_state_memex_guard("set_abandoned")
    from scripts import backend_local

    return backend_local.set_abandoned(task_id=task_id, category=category)


def set_abandoned_ack(db_path: str, task_id: int) -> dict:
    """Stamp `tasks.abandoned_ack_at` (PM/human ack of an abandoned task).
    Audit only — does not change status or gate wave dispatch."""
    _dispatch_state_memex_guard("set_abandoned_ack")
    from scripts import backend_local

    return backend_local.set_abandoned_ack(task_id=task_id)


# ── Reads ──────────────────────────────────────────────────────────────────


def get_task(db_path: str, task_id: int) -> dict | None:
    return backend.get_task(task_id=task_id)


def list_tasks(
    db_path: str,
    status: str | None = None,
    assigned_to: str | None = None,
    project_id: int | None = None,
) -> list[dict]:
    """List tasks, optionally filtered.

    `backend.list_tasks` requires `project_id` (spec §4.3) so the
    project-scoped facade can stay efficient (no full-table scan).
    `assigned_to` rides into the backend WHERE clause directly — no
    post-filter pass.

    When `project_id` is None, both modes fall back to a cross-project
    surface that bypasses `backend.list_tasks` (atelier#33):
      - Memex mode: routes to `backend_memex.list_tasks_cross_project`.
      - Local mode: reaches into `backend_local._conn()` for a direct
        full-table scan.

    Callers should still pass `project_id` whenever they have one —
    the project-scoped path is cheaper and is the spec §4.3 contract.
    """
    if project_id is not None:
        return backend.list_tasks(project_id=project_id, status=status, assigned_to=assigned_to)

    # No project filter — cross-project surface (atelier#33).
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        return backend_memex.list_tasks_cross_project(status=status, assigned_to=assigned_to)
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
            f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at",  # nosec B608
            params,
        ).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def search_tasks(
    db_path: str, query: str, status: str | None = None, assigned_to: str | None = None
) -> list[dict]:
    """LIKE-search across title / description / notes.

    No backend surface yet (FTS5 covers `project_documents` only — Plan 2
    Task 7 deliberately scoped FTS to the document table). Reach into
    the active backend's connection so the search still respects mode
    dispatch in Local mode.
    """
    if mode_detector.detect_mode() == "memex":
        # Memex-side task FTS lands with the v1.2 task domain; return empty rather than crash callers.
        return []
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
            f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at",  # nosec B608
            params,
        ).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import argparse
    import json
    import sys

    db_path = ".ai/memex.db"
    cmd = sys.argv[1]

    if cmd == "create":
        parser = argparse.ArgumentParser()
        parser.add_argument("project_id", type=int)
        parser.add_argument("title")
        parser.add_argument("created_by")
        parser.add_argument("--description")
        # `type=str` is explicit so `_coerce_priority` can accept either
        # the legacy TEXT form ("critical"|"high"|...) or stringified
        # ints ("0".."4") without ceremony.
        parser.add_argument("--priority", type=str, default="0")
        args = parser.parse_args(sys.argv[2:])
        print(
            json.dumps(
                create_task(
                    db_path,
                    project_id=args.project_id,
                    title=args.title,
                    created_by=args.created_by,
                    description=args.description,
                    priority=args.priority,
                ),
                indent=2,
            )
        )
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
        parser.add_argument("--priority", type=str)
        args = parser.parse_args(sys.argv[2:])
        kwargs = {k: v for k, v in vars(args).items() if k != "task_id" and v is not None}
        print(json.dumps(update_task(db_path, args.task_id, **kwargs), indent=2))
    elif cmd == "list":
        parser = argparse.ArgumentParser()
        parser.add_argument("--status")
        parser.add_argument("--assigned_to")
        parser.add_argument("--project_id", type=int)
        args = parser.parse_args(sys.argv[2:])
        print(
            json.dumps(
                list_tasks(
                    db_path,
                    status=args.status,
                    assigned_to=args.assigned_to,
                    project_id=args.project_id,
                ),
                indent=2,
            )
        )
    elif cmd == "search":
        parser = argparse.ArgumentParser()
        parser.add_argument("query")
        parser.add_argument("--status")
        parser.add_argument("--assigned_to")
        args = parser.parse_args(sys.argv[2:])
        print(
            json.dumps(
                search_tasks(db_path, args.query, status=args.status, assigned_to=args.assigned_to),
                indent=2,
            )
        )

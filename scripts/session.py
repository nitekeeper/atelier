# scripts/session.py
"""DB-backed PM session management — routed through the backend facade.

Plan 3 Task 5 rewires session writes through ``backend.upsert_session`` while
preserving the legacy public surface (``write_session``, ``read_latest``,
``list_sessions``, ``update_session``, ``prune_sessions``, ``get_session``)
so existing callers and the ``hooks/session_open.py`` hook stay unmodified.

The facade carries the overlap set (``phase``, ``current_tasks``,
``accomplished``, ``next_action``, ``status``, ``pm_notes``); the
schema-only fields (``pre_diagnose_phase``, ``blocking_reason``,
``opened_at``, ``closed_at``) are filled by a follow-up backend-direct
update so the v1.1.0 ``sessions`` schema stays fully populated.

Null-tolerance note (Nit-4): callers that surface meeting rows alongside
session rows must use ``meeting.get("filename") or ""`` because the v1.1.0
``meeting_minutes`` schema declares ``filename`` nullable. ``session.py``
itself does not consume meeting rows, but the convention is documented here
so the rewrite story for sibling modules (``meetings.py``, Task 4) stays
uniform.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from scripts import backend


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Backend access helpers ─────────────────────────────────────────────────


def _is_memex_mode() -> bool:
    from scripts import mode_detector

    return mode_detector.detect_mode() == "memex"


def _session_by_id(session_id: int) -> dict | None:
    """Backend-direct lookup of a single session row by primary key.

    The facade has no ``get_session`` (sessions are operational state, not
    documents); we drop to backend internals because callers — and tests —
    still want by-id access for ``update_session`` / ``get_session``.
    """
    if _is_memex_mode():
        from scripts import backend_memex

        rows = backend_memex._memex_core_query(
            store="atelier", table="sessions", where={"id": session_id}
        )
        return rows[0] if rows else None
    from scripts import backend_local

    c = backend_local._conn()
    try:
        row = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


def _patch_session(session_id: int, updates: dict) -> dict | None:
    """Apply schema-only updates that the facade ``upsert_session`` doesn't
    carry (``pre_diagnose_phase``, ``blocking_reason``, ``opened_at``,
    ``closed_at``). All values must already be column-shaped.
    """
    if not updates:
        return _session_by_id(session_id)
    updates = dict(updates)
    updates["updated_at"] = _now()
    if _is_memex_mode():
        from scripts import backend_memex

        backend_memex._memex_core_update(
            store="atelier", table="sessions", row_id=session_id, changes=updates
        )
        return _session_by_id(session_id)
    from scripts import backend_local

    c = backend_local._conn()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        c.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            (*updates.values(), session_id),
        )
        c.commit()
        row = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


def _close_in_progress_for_pair(project_id: int, agent_id: str) -> None:
    """Force any existing ``status='in-progress'`` row for ``(project_id,
    agent_id)`` to ``status='complete'`` so the next ``upsert_session``
    INSERTs instead of UPDATEs.

    ``backend.upsert_session`` upserts against the lone in-progress row;
    the legacy ``write_session`` contract is one-INSERT-per-call. We bridge
    by closing the prior in-progress row before each insert.
    """
    if _is_memex_mode():
        from scripts import backend_memex

        rows = backend_memex._memex_core_query(
            store="atelier",
            table="sessions",
            where={"project_id": project_id, "agent_id": agent_id, "status": "in-progress"},
        )
        for row in rows:
            backend_memex._memex_core_update(
                store="atelier",
                table="sessions",
                row_id=row["id"],
                changes={"status": "complete", "updated_at": _now()},
            )
        return
    from scripts import backend_local

    c = backend_local._conn()
    try:
        c.execute(
            "UPDATE sessions SET status = 'complete', updated_at = ? "
            "WHERE project_id = ? AND agent_id = ? AND status = 'in-progress'",
            (_now(), project_id, agent_id),
        )
        c.commit()
    finally:
        c.close()


# ── Public surface ─────────────────────────────────────────────────────────


def write_session(
    db_path: str,
    project_id: int,
    agent_id: str,
    phase: str,
    status: str,
    *,
    pre_diagnose_phase: str | None = None,
    current_tasks: str | None = None,
    accomplished: str | None = None,
    next_action: str | None = None,
    blocking_reason: str | None = None,
    pm_notes: str | None = None,
    opened_at: str | None = None,
) -> dict:
    """Insert a new session row and return it.

    ``db_path`` is retained for signature parity — the backend resolves the
    DB location via ``mode_detector`` + ``_workspace_root``.
    """
    # Close any open session for this pair so we always INSERT below.
    _close_in_progress_for_pair(project_id, agent_id)
    row = backend.upsert_session(
        project_id=project_id,
        agent_id=agent_id,
        phase=phase,
        current_tasks=current_tasks,
        accomplished=accomplished,
        next_action=next_action,
        status=status,
        pm_notes=pm_notes,
    )
    # Fill in the schema-only fields the facade doesn't carry.
    extras: dict = {}
    if pre_diagnose_phase is not None:
        extras["pre_diagnose_phase"] = pre_diagnose_phase
    if blocking_reason is not None:
        extras["blocking_reason"] = blocking_reason
    extras["opened_at"] = opened_at or _now()
    if extras:
        row = _patch_session(row["id"], extras) or row
    return row


def get_session(db_path: str, session_id: int, *, conn=None) -> dict | None:
    """Fetch a session by ID. ``conn`` kept for back-compat (ignored)."""
    return _session_by_id(session_id)


def read_latest(db_path: str, project_id: int) -> dict | None:
    """Return the most recent session for a project, or None."""
    if _is_memex_mode():
        from scripts import backend_memex

        # Equality-only `where=` per spec §6.2; sort + limit applied here.
        rows = backend_memex._memex_core_query(
            store="atelier", table="sessions", where={"project_id": project_id}
        )
        if not rows:
            return None
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows[0]
    from scripts import backend_local

    c = backend_local._conn()
    try:
        row = c.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


def list_sessions(db_path: str, project_id: int, limit: int = 10) -> list[dict]:
    """Return sessions for a project, most recent first."""
    if _is_memex_mode():
        from scripts import backend_memex

        rows = backend_memex._memex_core_query(
            store="atelier", table="sessions", where={"project_id": project_id}
        )
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows[:limit]
    from scripts import backend_local

    c = backend_local._conn()
    try:
        rows = c.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def update_session(db_path: str, session_id: int, **kwargs) -> dict:
    """Update allowed fields on a session row.

    Routes the overlap-set fields through ``backend.upsert_session`` and
    the schema-only fields through ``_patch_session``. Unknown fields are
    silently ignored (legacy contract).
    """
    allowed = {
        "phase",
        "pre_diagnose_phase",
        "current_tasks",
        "accomplished",
        "next_action",
        "status",
        "blocking_reason",
        "pm_notes",
        "opened_at",
        "closed_at",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return _session_by_id(session_id) or {}

    existing = _session_by_id(session_id)
    if existing is None:
        raise ValueError(f"session_id={session_id} not found")

    overlap = {"phase", "current_tasks", "accomplished", "next_action", "status", "pm_notes"}
    facade_changes = {k: v for k, v in updates.items() if k in overlap}
    schema_only = {k: v for k, v in updates.items() if k not in overlap}

    if facade_changes:
        # Facade upserts against the in-progress row for the pair. If the
        # existing row is already in-progress, the upsert lands on it; if
        # it is closed, the facade would INSERT a new row instead, which
        # is NOT what `update_session(id=...)` means. So when the row is
        # not in-progress we go backend-direct.
        if existing.get("status") == "in-progress":
            backend.upsert_session(
                project_id=existing["project_id"],
                agent_id=existing["agent_id"],
                phase=facade_changes.get("phase"),
                current_tasks=facade_changes.get("current_tasks"),
                accomplished=facade_changes.get("accomplished"),
                next_action=facade_changes.get("next_action"),
                status=facade_changes.get("status", "in-progress"),
                pm_notes=facade_changes.get("pm_notes"),
            )
        else:
            schema_only.update(facade_changes)

    if schema_only:
        _patch_session(session_id, schema_only)

    return _session_by_id(session_id) or {}


def prune_sessions(db_path: str, project_id: int, keep: int) -> int:
    """Delete oldest sessions for project, keeping the N most recent.
    Returns count deleted.
    """
    if _is_memex_mode():
        from scripts import backend_memex

        memex_stores = backend_memex._memex_module("stores")
        rows = backend_memex._memex_core_query(
            store="atelier", table="sessions", where={"project_id": project_id}
        )
        rows.sort(key=lambda r: r["id"], reverse=True)
        keep_ids = {r["id"] for r in rows[:keep]}
        deleted = 0
        for r in rows:
            if r["id"] not in keep_ids:
                memex_stores.delete(name="atelier", table="sessions", row_id=r["id"])
                deleted += 1
        return deleted
    from scripts import backend_local

    c = backend_local._conn()
    try:
        keep_ids = [
            row[0]
            for row in c.execute(
                "SELECT id FROM sessions WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (project_id, keep),
            ).fetchall()
        ]
        if not keep_ids:
            return 0
        placeholders = ",".join("?" * len(keep_ids))
        cur = c.execute(
            f"DELETE FROM sessions WHERE project_id = ? AND id NOT IN ({placeholders})",
            (project_id, *keep_ids),
        )
        c.commit()
        return cur.rowcount
    finally:
        c.close()


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    db_path = ".ai/memex.db"
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "write":
        parser = argparse.ArgumentParser()
        parser.add_argument("project_id", type=int)
        parser.add_argument("agent_id")
        parser.add_argument("phase")
        parser.add_argument("status")
        parser.add_argument("--pre-diagnose-phase")
        parser.add_argument("--current-tasks")
        parser.add_argument("--accomplished")
        parser.add_argument("--next-action")
        parser.add_argument("--blocking-reason")
        parser.add_argument("--notes")
        args = parser.parse_args(sys.argv[2:])
        session = write_session(
            db_path,
            args.project_id,
            args.agent_id,
            args.phase,
            args.status,
            pre_diagnose_phase=args.pre_diagnose_phase,
            current_tasks=args.current_tasks,
            accomplished=args.accomplished,
            next_action=args.next_action,
            blocking_reason=args.blocking_reason,
            pm_notes=args.notes,
        )
        print(json.dumps(session, indent=2))

    elif cmd == "read-latest":
        project_id = int(sys.argv[2])
        result = read_latest(db_path, project_id)
        if result:
            print(json.dumps(result, indent=2))
        else:
            print("No session found for this project.")

    elif cmd == "list":
        parser = argparse.ArgumentParser()
        parser.add_argument("project_id", type=int)
        parser.add_argument("--limit", type=int, default=10)
        args = parser.parse_args(sys.argv[2:])
        print(
            json.dumps(
                list_sessions(db_path, args.project_id, limit=args.limit),
                indent=2,
            )
        )

    elif cmd == "update":
        parser = argparse.ArgumentParser()
        parser.add_argument("session_id", type=int)
        parser.add_argument("--phase")
        parser.add_argument("--status")
        parser.add_argument("--accomplished")
        parser.add_argument("--next-action")
        parser.add_argument("--notes")
        parser.add_argument("--closed-at")
        args = parser.parse_args(sys.argv[2:])
        kwargs = {}
        if args.phase:
            kwargs["phase"] = args.phase
        if args.status:
            kwargs["status"] = args.status
        if args.accomplished:
            kwargs["accomplished"] = args.accomplished
        if args.next_action:
            kwargs["next_action"] = args.next_action
        if args.notes:
            kwargs["pm_notes"] = args.notes
        if args.closed_at:
            kwargs["closed_at"] = args.closed_at
        print(json.dumps(update_session(db_path, args.session_id, **kwargs), indent=2))

    elif cmd == "prune":
        parser = argparse.ArgumentParser()
        parser.add_argument("project_id", type=int)
        parser.add_argument("--keep", type=int, default=5)
        args = parser.parse_args(sys.argv[2:])
        deleted = prune_sessions(db_path, args.project_id, keep=args.keep)
        print(f"Pruned {deleted} session(s), kept {args.keep} most recent.")

    else:
        print("Commands: write, read-latest, list, update, prune")
        sys.exit(1)

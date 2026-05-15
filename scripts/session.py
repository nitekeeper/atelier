# scripts/session.py
"""DB-backed PM session management.

Each session is one row in the sessions table. PM writes at session close,
reads at session open. Old sessions are prunable; git retains full history.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(cur, row: tuple) -> dict:
    return dict(zip([col[0] for col in cur.description], row))


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
    """Insert a new session row and return it."""
    now = _now()
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO sessions
               (project_id, agent_id, phase, pre_diagnose_phase, current_tasks,
                accomplished, next_action, status, blocking_reason, pm_notes,
                opened_at, closed_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (project_id, agent_id, phase, pre_diagnose_phase, current_tasks,
             accomplished, next_action, status, blocking_reason, pm_notes,
             opened_at or now, now, now),
        )
        conn.commit()
        session_id = cur.lastrowid
        result = get_session(db_path, session_id, conn=conn)
        return result
    finally:
        conn.close()


def get_session(db_path: str, session_id: int, *, conn=None) -> dict | None:
    """Fetch a session by ID."""
    _close = conn is None
    if conn is None:
        conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None
    finally:
        if _close:
            conn.close()


def read_latest(db_path: str, project_id: int) -> dict | None:
    """Return the most recent session for a project, or None."""
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (project_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None
    finally:
        conn.close()


def list_sessions(db_path: str, project_id: int, limit: int = 10) -> list[dict]:
    """Return sessions for a project, most recent first."""
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
        rows = cur.fetchall()
        cols = [col[0] for col in cur.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def update_session(db_path: str, session_id: int, **kwargs) -> dict:
    """Update allowed fields on a session row."""
    allowed = {
        "phase", "pre_diagnose_phase", "current_tasks", "accomplished",
        "next_action", "status", "blocking_reason", "pm_notes",
        "opened_at", "closed_at",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_session(db_path, session_id)
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            (*updates.values(), session_id),
        )
        conn.commit()
        return get_session(db_path, session_id, conn=conn)
    finally:
        conn.close()


def prune_sessions(db_path: str, project_id: int, keep: int) -> int:
    """Delete oldest sessions for project, keeping the N most recent. Returns count deleted."""
    conn = get_connection(db_path)
    try:
        keep_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM sessions WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (project_id, keep),
            ).fetchall()
        ]
        if not keep_ids:
            return 0
        placeholders = ",".join("?" * len(keep_ids))
        cur = conn.execute(
            f"DELETE FROM sessions WHERE project_id = ? AND id NOT IN ({placeholders})",
            (project_id, *keep_ids),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    import json

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
            db_path, args.project_id, args.agent_id, args.phase, args.status,
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
        print(json.dumps(list_sessions(db_path, args.project_id, limit=args.limit), indent=2))

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
        if args.phase:         kwargs["phase"] = args.phase
        if args.status:        kwargs["status"] = args.status
        if args.accomplished:  kwargs["accomplished"] = args.accomplished
        if args.next_action:   kwargs["next_action"] = args.next_action
        if args.notes:         kwargs["pm_notes"] = args.notes
        if args.closed_at:     kwargs["closed_at"] = args.closed_at
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

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
    (content-addressable archive, not human-browsable workspace state).

    Single-writer policy: this function owns the .md file. In Local
    mode we bypass the facade and call `backend_local.write_meeting`
    directly with `skip_md=True` so its participants-blind renderer
    cannot clobber the participants-aware file we just wrote (I1 from
    round-1 review). Memex mode has no on-disk renderer in the
    backend, so the facade is safe there."""
    filename = _meeting_filename(date, title)
    file_path = Path(meetings_dir) / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        _render_meeting_md(title, date, summary, decisions, participants),
        encoding="utf-8",
    )
    if mode_detector.detect_mode() == "memex":
        result = backend.write_meeting(
            workspace_id=workspace_id, project_id=project_id,
            title=title, date=date, summary=summary, decisions=decisions,
            subdomain=subdomain, created_by=created_by,
        )
    else:
        # Direct backend_local call so we can pass skip_md=True (the
        # facade doesn't accept skip_md — the kwarg is a Local-only
        # implementation detail).
        from scripts import backend_local
        result = backend_local.write_meeting(
            workspace_id=workspace_id, project_id=project_id,
            title=title, date=date, summary=summary, decisions=decisions,
            subdomain=subdomain, created_by=created_by,
            skip_md=True,
        )
    now = _now()
    return {
        "id": result["row_id"], "title": title, "date": date,
        "filename": filename,
        "summary": summary, "decisions": decisions,
        "created_by": created_by,
        "created_at": now, "updated_at": now,
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
        # No row_id-based delete here (composite primary key). Use the
        # dedicated execute helper from backend_memex — `stores.query()`
        # is SELECT-only and won't accept DELETE.
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
        # Two-step (participants DELETE, then meeting DELETE) is NOT
        # wrapped in a transaction here because Memex Core's API splits
        # writes across two helpers: `_memex_core_execute` (raw SQL,
        # needed for composite-key DELETE on meeting_participants) and
        # `stores.delete(row_id=...)` (row-id-keyed, the only way to hit
        # Memex's Index/Archivist hooks for meeting_minutes). The two
        # paths don't share a connection, so a transaction across them
        # isn't available. Worst case: participants DELETE succeeds and
        # the meeting DELETE fails — re-running delete_meeting is
        # idempotent (orphan participants don't exist because we deleted
        # them; the meeting row is still there, so caller can retry).
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
    # backend_local stores `filename` as workspace-relative
    # (`.ai/meetings/<date>-<slug>.md`); joining with `meetings_dir`
    # directly would produce a nonexistent nested path. Take just the
    # basename so callers can pass either the bare meetings_dir or a
    # workspace_root-rooted Path interchangeably.
    md_file = Path(meetings_dir) / Path(meeting["filename"]).name
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


if __name__ == "__main__":
    import sys
    import json
    import argparse

    db_path = ".ai/atelier.db"
    cmd = sys.argv[1]

    if cmd == "create":
        parser = argparse.ArgumentParser()
        parser.add_argument("title")
        parser.add_argument("date")
        parser.add_argument("summary")
        parser.add_argument("decisions")
        parser.add_argument("created_by")
        parser.add_argument("--meetings-dir", default=".ai/meetings")
        parser.add_argument("--workspace-id", type=int, default=None)
        parser.add_argument("--project-id", type=int, default=None)
        # TODO(N3): expose --subdomain (str) and --participants
        # (comma-separated agent ids) on this CLI surface. The Python
        # API already accepts them; the CLI wrapper just doesn't plumb
        # them through yet. Low priority — most callers use the Python
        # API directly via skill files.
        args = parser.parse_args(sys.argv[2:])
        result = create_meeting(db_path, Path(args.meetings_dir), title=args.title,
                                date=args.date, summary=args.summary,
                                decisions=args.decisions, created_by=args.created_by,
                                workspace_id=args.workspace_id,
                                project_id=args.project_id)
        print(json.dumps(result, indent=2))
    elif cmd == "get":
        result = get_meeting(db_path, int(sys.argv[2]))
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "update":
        parser = argparse.ArgumentParser()
        parser.add_argument("meeting_id", type=int)
        parser.add_argument("--title")
        parser.add_argument("--date")
        parser.add_argument("--summary")
        parser.add_argument("--decisions")
        args = parser.parse_args(sys.argv[2:])
        kwargs = {k: v for k, v in vars(args).items() if k != "meeting_id" and v is not None}
        print(json.dumps(update_meeting(db_path, args.meeting_id, **kwargs), indent=2))
    elif cmd == "delete":
        parser = argparse.ArgumentParser()
        parser.add_argument("meeting_id", type=int)
        parser.add_argument("--meetings-dir", default=".ai/meetings")
        args = parser.parse_args(sys.argv[2:])
        print("Deleted" if delete_meeting(db_path, Path(args.meetings_dir), args.meeting_id) else "Not found")
    elif cmd == "list":
        print(json.dumps(list_meetings(db_path), indent=2))
    elif cmd == "search":
        print(json.dumps(search_meetings(db_path, sys.argv[2]), indent=2))
    elif cmd == "add-participant":
        add_participant(db_path, int(sys.argv[2]), sys.argv[3])
        print("Participant added.")
    elif cmd == "remove-participant":
        remove_participant(db_path, int(sys.argv[2]), sys.argv[3])
        print("Participant removed.")
    elif cmd == "participants":
        print(json.dumps(get_participants(db_path, int(sys.argv[2])), indent=2))

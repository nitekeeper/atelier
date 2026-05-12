# scripts/meetings.py
import re
from datetime import datetime, timezone
from pathlib import Path
from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _md_filename(date: str, title: str) -> str:
    return f"{date}-{_slugify(title)}.md"


def _write_md(meetings_dir: Path, filename: str, title: str,
              date: str, summary: str, decisions: str) -> None:
    meetings_dir.mkdir(parents=True, exist_ok=True)
    content = f"# {title}\n\nDate: {date}\n\n## Summary\n\n{summary}\n\n## Decisions\n\n{decisions}\n"
    (meetings_dir / filename).write_text(content)


def create_meeting(db_path: str, meetings_dir: Path, title: str, date: str,
                   summary: str, decisions: str, created_by: str) -> dict:
    filename = _md_filename(date, title)
    _write_md(meetings_dir, filename, title, date, summary, decisions)
    now = _now()
    conn = get_connection(db_path)
    cur = conn.execute(
        "INSERT INTO meeting_minutes (title, date, filename, summary, decisions, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (title, date, filename, summary, decisions, created_by, now, now)
    )
    conn.commit()
    meeting = get_meeting(db_path, cur.lastrowid)
    conn.close()
    return meeting


def get_meeting(db_path: str, meeting_id: int) -> dict | None:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM meeting_minutes WHERE id = ?", (meeting_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return dict(zip([col[0] for col in cur.description], row))


def update_meeting(db_path: str, meeting_id: int, **kwargs) -> dict:
    allowed = {"title", "date", "summary", "decisions"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection(db_path)
    conn.execute(
        f"UPDATE meeting_minutes SET {set_clause} WHERE id = ?",
        (*updates.values(), meeting_id)
    )
    conn.commit()
    conn.close()
    return get_meeting(db_path, meeting_id)


def delete_meeting(db_path: str, meetings_dir: Path, meeting_id: int) -> bool:
    meeting = get_meeting(db_path, meeting_id)
    if meeting is None:
        return False
    md_file = meetings_dir / meeting["filename"]
    if md_file.exists():
        md_file.unlink()
    conn = get_connection(db_path)
    conn.execute("DELETE FROM meeting_participants WHERE meeting_id = ?", (meeting_id,))
    cur = conn.execute("DELETE FROM meeting_minutes WHERE id = ?", (meeting_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_meetings(db_path: str) -> list[dict]:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM meeting_minutes ORDER BY date DESC")
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def search_meetings(db_path: str, query: str) -> list[dict]:
    pattern = f"%{query}%"
    conn = get_connection(db_path)
    cur = conn.execute(
        "SELECT * FROM meeting_minutes WHERE title LIKE ? OR summary LIKE ? OR decisions LIKE ? ORDER BY date DESC",
        (pattern, pattern, pattern)
    )
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def add_participant(db_path: str, meeting_id: int, agent_id: str) -> None:
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO meeting_participants (meeting_id, agent_id) VALUES (?, ?)",
        (meeting_id, agent_id)
    )
    conn.commit()
    conn.close()


def remove_participant(db_path: str, meeting_id: int, agent_id: str) -> None:
    conn = get_connection(db_path)
    conn.execute(
        "DELETE FROM meeting_participants WHERE meeting_id = ? AND agent_id = ?",
        (meeting_id, agent_id)
    )
    conn.commit()
    conn.close()


def get_participants(db_path: str, meeting_id: int) -> list[dict]:
    conn = get_connection(db_path)
    cur = conn.execute(
        "SELECT a.* FROM agents a JOIN meeting_participants mp ON a.id = mp.agent_id WHERE mp.meeting_id = ?",
        (meeting_id,)
    )
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


if __name__ == "__main__":
    import sys
    import json
    import argparse

    db_path = ".ai/memex.db"
    cmd = sys.argv[1]

    if cmd == "create":
        parser = argparse.ArgumentParser()
        parser.add_argument("title")
        parser.add_argument("date")
        parser.add_argument("summary")
        parser.add_argument("decisions")
        parser.add_argument("created_by")
        parser.add_argument("--meetings-dir", default=".ai/meetings")
        args = parser.parse_args(sys.argv[2:])
        result = create_meeting(db_path, Path(args.meetings_dir), title=args.title,
                                date=args.date, summary=args.summary,
                                decisions=args.decisions, created_by=args.created_by)
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
    elif cmd == "participants":
        print(json.dumps(get_participants(db_path, int(sys.argv[2])), indent=2))

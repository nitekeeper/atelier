# scripts/projects.py
from datetime import datetime, timezone
from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_project(db_path: str, name: str, description: str | None,
                   created_by: str, repo: str | None = None) -> dict:
    now = _now()
    conn = get_connection(db_path)
    cur = conn.execute(
        "INSERT INTO projects (name, description, repo, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name, description, repo, created_by, now, now)
    )
    conn.commit()
    project = get_project(db_path, cur.lastrowid)
    conn.close()
    return project


def get_project(db_path: str, project_id: int) -> dict | None:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return dict(zip([col[0] for col in cur.description], row))


def update_project(db_path: str, project_id: int, **kwargs) -> dict:
    allowed = {"name", "description", "repo", "phase"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection(db_path)
    conn.execute(
        f"UPDATE projects SET {set_clause} WHERE id = ?",
        (*updates.values(), project_id)
    )
    conn.commit()
    conn.close()
    return get_project(db_path, project_id)


def delete_project(db_path: str, project_id: int) -> bool:
    conn = get_connection(db_path)
    cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_projects(db_path: str, phase: str | None = None) -> list[dict]:
    conn = get_connection(db_path)
    if phase:
        cur = conn.execute("SELECT * FROM projects WHERE phase = ? ORDER BY name", (phase,))
    else:
        cur = conn.execute("SELECT * FROM projects ORDER BY name")
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def search_projects(db_path: str, query: str) -> list[dict]:
    pattern = f"%{query}%"
    conn = get_connection(db_path)
    cur = conn.execute(
        "SELECT * FROM projects WHERE name LIKE ? OR description LIKE ? ORDER BY name",
        (pattern, pattern)
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
        parser.add_argument("name")
        parser.add_argument("description")
        parser.add_argument("created_by")
        parser.add_argument("--repo")
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(create_project(db_path, name=args.name, description=args.description,
                                         created_by=args.created_by, repo=args.repo), indent=2))
    elif cmd == "get":
        result = get_project(db_path, int(sys.argv[2]))
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "update":
        parser = argparse.ArgumentParser()
        parser.add_argument("project_id", type=int)
        parser.add_argument("--name")
        parser.add_argument("--description")
        parser.add_argument("--phase")
        parser.add_argument("--repo")
        args = parser.parse_args(sys.argv[2:])
        kwargs = {k: v for k, v in vars(args).items() if k != "project_id" and v is not None}
        print(json.dumps(update_project(db_path, args.project_id, **kwargs), indent=2))
    elif cmd == "delete":
        print("Deleted" if delete_project(db_path, int(sys.argv[2])) else "Not found")
    elif cmd == "list":
        parser = argparse.ArgumentParser()
        parser.add_argument("--phase")
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(list_projects(db_path, phase=args.phase), indent=2))
    elif cmd == "search":
        print(json.dumps(search_projects(db_path, sys.argv[2]), indent=2))

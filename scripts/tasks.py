# scripts/tasks.py
from datetime import datetime, timezone
from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_task(db_path: str, project_id: int | None, title: str,
                created_by: str, description: str | None = None,
                priority: int = 0) -> dict:
    now = _now()
    conn = get_connection(db_path)
    cur = conn.execute(
        "INSERT INTO tasks (project_id, title, description, created_by, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, title, description, created_by, priority, now, now)
    )
    conn.commit()
    task = get_task(db_path, cur.lastrowid)
    conn.close()
    return task


def get_task(db_path: str, task_id: int) -> dict | None:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return dict(zip([col[0] for col in cur.description], row))


def update_task(db_path: str, task_id: int, **kwargs) -> dict:
    allowed = {"title", "description", "priority", "notes", "status", "assigned_to"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection(db_path)
    conn.execute(
        f"UPDATE tasks SET {set_clause} WHERE id = ?",
        (*updates.values(), task_id)
    )
    conn.commit()
    conn.close()
    return get_task(db_path, task_id)


def delete_task(db_path: str, task_id: int) -> bool:
    conn = get_connection(db_path)
    cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def assign_task(db_path: str, task_id: int, agent_id: str) -> dict:
    return update_task(db_path, task_id, assigned_to=agent_id, status="assigned")


def claim_task(db_path: str, task_id: int, agent_id: str) -> dict:
    task = get_task(db_path, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    if task["assigned_to"] != agent_id:
        raise ValueError(f"Task {task_id} is not assigned to {agent_id}")
    return update_task(db_path, task_id, status="in-progress")


def complete_task(db_path: str, task_id: int) -> dict:
    return update_task(db_path, task_id, status="complete")


def list_tasks(db_path: str, status: str | None = None,
               assigned_to: str | None = None,
               project_id: int | None = None) -> list[dict]:
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if assigned_to:
        conditions.append("assigned_to = ?")
        params.append(assigned_to)
    if project_id is not None:
        conditions.append("project_id = ?")
        params.append(project_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = get_connection(db_path)
    cur = conn.execute(f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at", params)
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def search_tasks(db_path: str, query: str,
                 status: str | None = None,
                 assigned_to: str | None = None) -> list[dict]:
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
    conn = get_connection(db_path)
    cur = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at", params
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
        parser.add_argument("project_id", type=int)
        parser.add_argument("title")
        parser.add_argument("created_by")
        parser.add_argument("--description")
        parser.add_argument("--priority", type=int, default=0)
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
        parser.add_argument("--priority", type=int)
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

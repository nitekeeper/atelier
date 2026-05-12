from datetime import datetime, timezone
from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_role(db_path: str, name: str, description: str) -> dict:
    conn = get_connection(db_path)
    now = _now()
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, description, now, now)
    )
    conn.commit()
    role = get_role(db_path, cur.lastrowid)
    conn.close()
    return role


def get_role(db_path: str, role_id: int) -> dict | None:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return dict(zip([col[0] for col in cur.description], row))


def update_role(db_path: str, role_id: int, **kwargs) -> dict:
    allowed = {"name", "description"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection(db_path)
    conn.execute(
        f"UPDATE roles SET {set_clause} WHERE id = ?",
        (*updates.values(), role_id)
    )
    conn.commit()
    conn.close()
    return get_role(db_path, role_id)


def delete_role(db_path: str, role_id: int) -> bool:
    conn = get_connection(db_path)
    cur = conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_roles(db_path: str) -> list[dict]:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM roles ORDER BY name")
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def search_roles(db_path: str, query: str) -> list[dict]:
    pattern = f"%{query}%"
    conn = get_connection(db_path)
    cur = conn.execute(
        "SELECT * FROM roles WHERE name LIKE ? OR description LIKE ? ORDER BY name",
        (pattern, pattern)
    )
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


if __name__ == "__main__":
    import sys
    import json

    db_path = ".ai/memex.db"
    cmd = sys.argv[1]

    if cmd == "create":
        print(json.dumps(create_role(db_path, name=sys.argv[2], description=sys.argv[3]), indent=2))
    elif cmd == "get":
        result = get_role(db_path, int(sys.argv[2]))
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "update":
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("role_id", type=int)
        parser.add_argument("--name")
        parser.add_argument("--description")
        args = parser.parse_args(sys.argv[2:])
        kwargs = {k: v for k, v in vars(args).items() if k != "role_id" and v is not None}
        print(json.dumps(update_role(db_path, args.role_id, **kwargs), indent=2))
    elif cmd == "delete":
        print("Deleted" if delete_role(db_path, int(sys.argv[2])) else "Not found")
    elif cmd == "list":
        print(json.dumps(list_roles(db_path), indent=2))
    elif cmd == "search":
        print(json.dumps(search_roles(db_path, sys.argv[2]), indent=2))

from datetime import datetime, timezone
from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_agent(db_path: str, id: str, name: str, role_id: int, profile: str) -> dict:
    now = _now()
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (id, name, role_id, profile, now, now)
    )
    conn.commit()
    conn.close()
    return get_agent(db_path, id)


def get_agent(db_path: str, agent_id: str) -> dict | None:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return dict(zip([col[0] for col in cur.description], row))


def update_agent(db_path: str, agent_id: str, **kwargs) -> dict:
    allowed = {"name", "role_id", "profile"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection(db_path)
    conn.execute(
        f"UPDATE agents SET {set_clause} WHERE id = ?",
        (*updates.values(), agent_id)
    )
    conn.commit()
    conn.close()
    return get_agent(db_path, agent_id)


def delete_agent(db_path: str, agent_id: str) -> bool:
    conn = get_connection(db_path)
    cur = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_agents(db_path: str, role_id: int | None = None) -> list[dict]:
    conn = get_connection(db_path)
    if role_id is not None:
        cur = conn.execute("SELECT * FROM agents WHERE role_id = ? ORDER BY name", (role_id,))
    else:
        cur = conn.execute("SELECT * FROM agents ORDER BY name")
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def search_agents(db_path: str, query: str, role_id: int | None = None) -> list[dict]:
    pattern = f"%{query}%"
    conn = get_connection(db_path)
    if role_id is not None:
        cur = conn.execute(
            "SELECT * FROM agents WHERE role_id = ? AND (name LIKE ? OR profile LIKE ?) ORDER BY name",
            (role_id, pattern, pattern)
        )
    else:
        cur = conn.execute(
            "SELECT * FROM agents WHERE name LIKE ? OR profile LIKE ? ORDER BY name",
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
        print(json.dumps(create_agent(db_path, id=sys.argv[2], name=sys.argv[3],
                                       role_id=int(sys.argv[4]), profile=sys.argv[5]), indent=2))
    elif cmd == "get":
        result = get_agent(db_path, sys.argv[2])
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "update":
        parser = argparse.ArgumentParser()
        parser.add_argument("agent_id")
        parser.add_argument("--name")
        parser.add_argument("--role_id", type=int)
        parser.add_argument("--profile")
        args = parser.parse_args(sys.argv[2:])
        kwargs = {k: v for k, v in vars(args).items() if k != "agent_id" and v is not None}
        print(json.dumps(update_agent(db_path, args.agent_id, **kwargs), indent=2))
    elif cmd == "delete":
        print("Deleted" if delete_agent(db_path, sys.argv[2]) else "Not found")
    elif cmd == "list":
        parser = argparse.ArgumentParser()
        parser.add_argument("--role_id", type=int)
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(list_agents(db_path, role_id=args.role_id), indent=2))
    elif cmd == "search":
        parser = argparse.ArgumentParser()
        parser.add_argument("query")
        parser.add_argument("--role_id", type=int)
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(search_agents(db_path, args.query, role_id=args.role_id), indent=2))

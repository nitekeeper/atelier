# scripts/documents.py
from datetime import datetime, timezone
from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_document(db_path: str, project_id: int, type: str,
                    title: str, filename: str, created_by: str) -> dict:
    now = _now()
    conn = get_connection(db_path)
    cur = conn.execute(
        "INSERT INTO project_documents (project_id, type, title, filename, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, type, title, filename, created_by, now, now)
    )
    conn.commit()
    doc = get_document(db_path, cur.lastrowid)
    conn.close()
    return doc


def get_document(db_path: str, doc_id: int) -> dict | None:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM project_documents WHERE id = ?", (doc_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return dict(zip([col[0] for col in cur.description], row))


def update_document(db_path: str, doc_id: int, **kwargs) -> dict:
    allowed = {"type", "title", "filename"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection(db_path)
    conn.execute(
        f"UPDATE project_documents SET {set_clause} WHERE id = ?",
        (*updates.values(), doc_id)
    )
    conn.commit()
    conn.close()
    return get_document(db_path, doc_id)


def delete_document(db_path: str, doc_id: int) -> bool:
    conn = get_connection(db_path)
    cur = conn.execute("DELETE FROM project_documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_documents(db_path: str, project_id: int | None = None,
                   type: str | None = None) -> list[dict]:
    conditions, params = [], []
    if project_id is not None:
        conditions.append("project_id = ?")
        params.append(project_id)
    if type is not None:
        conditions.append("type = ?")
        params.append(type)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = get_connection(db_path)
    cur = conn.execute(f"SELECT * FROM project_documents {where} ORDER BY title", params)
    rows = cur.fetchall()
    cols = [col[0] for col in cur.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def search_documents(db_path: str, query: str,
                     project_id: int | None = None) -> list[dict]:
    pattern = f"%{query}%"
    conditions = ["(title LIKE ? OR type LIKE ?)"]
    params = [pattern, pattern]
    if project_id is not None:
        conditions.append("project_id = ?")
        params.append(project_id)
    where = "WHERE " + " AND ".join(conditions)
    conn = get_connection(db_path)
    cur = conn.execute(
        f"SELECT * FROM project_documents {where} ORDER BY title", params
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
        print(json.dumps(create_document(db_path, project_id=int(sys.argv[2]),
                                          type=sys.argv[3], title=sys.argv[4],
                                          filename=sys.argv[5], created_by=sys.argv[6]), indent=2))
    elif cmd == "get":
        result = get_document(db_path, int(sys.argv[2]))
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "update":
        parser = argparse.ArgumentParser()
        parser.add_argument("doc_id", type=int)
        parser.add_argument("--title")
        parser.add_argument("--type")
        parser.add_argument("--filename")
        args = parser.parse_args(sys.argv[2:])
        kwargs = {k: v for k, v in vars(args).items() if k != "doc_id" and v is not None}
        print(json.dumps(update_document(db_path, args.doc_id, **kwargs), indent=2))
    elif cmd == "delete":
        print("Deleted" if delete_document(db_path, int(sys.argv[2])) else "Not found")
    elif cmd == "list":
        parser = argparse.ArgumentParser()
        parser.add_argument("--project_id", type=int)
        parser.add_argument("--type")
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(list_documents(db_path, project_id=args.project_id, type=args.type), indent=2))
    elif cmd == "search":
        parser = argparse.ArgumentParser()
        parser.add_argument("query")
        parser.add_argument("--project_id", type=int)
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(search_documents(db_path, args.query, project_id=args.project_id), indent=2))

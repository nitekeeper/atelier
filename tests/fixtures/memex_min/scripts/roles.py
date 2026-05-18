"""roles CRUD — minimal stub. Trimmed copy of memex/scripts/roles.py."""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_role(db_path: str, name: str, description: str) -> dict:
    conn = get_connection(db_path)
    now = _now()
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (name, description, now, now),
    )
    conn.commit()
    role_id = cur.lastrowid
    conn.close()
    return get_role(db_path, role_id)


def get_role(db_path: str, role_id: int) -> dict | None:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def list_roles(db_path: str) -> list[dict]:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM roles ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

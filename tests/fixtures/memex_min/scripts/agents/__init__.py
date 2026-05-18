"""agents CRUD — minimal stub. Trimmed copy of memex/scripts/agents/__init__.py."""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_agent(db_path: str, agent_id: str, name: str, role_id: int,
                 profile: str) -> dict:
    conn = get_connection(db_path)
    now = _now()
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (agent_id, name, role_id, profile, now, now),
    )
    conn.commit()
    conn.close()
    return get_agent(db_path, agent_id)


def get_agent(db_path: str, agent_id: str) -> dict | None:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

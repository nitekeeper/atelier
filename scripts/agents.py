"""Agents — mode-conditional thin wrapper.

Local mode reads/writes the local Atelier `agents` table directly via
`scripts.db.get_connection(db_path)`. Memex mode forwards to Memex's
`scripts.agents` package against the registered `agents` store, loaded
through `backend_memex._memex_module` to sidestep the
`scripts/agents.py` ↔ Memex `scripts/agents/` package namespace
collision (resolved in Wave 1 T8-10 via importlib.util).

This module is intentionally retained as a thin wrapper rather than
deleted: ~12 call sites import `scripts.agents.create_agent` directly
with the legacy `(db_path, id=..., name=..., role_id=..., profile=...)`
signature. Rewiring through the mode-aware facade keeps every existing
call site working in both Local and Memex modes — `db_path` is honored
in Local mode (caller picks the file), and routed to
`~/.memex/agents.db` (via the public Memex registry) in Memex mode.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts import mode_detector
from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Memex-mode primitives ────────────────────────────────────────────────
#
# All Memex calls route through `backend_memex._memex_module(...)` —
# never `from scripts import agents as ...`, which would resolve to
# THIS module due to `sys.modules` caching. The legacy
# `backend_memex._ensure_memex_importable` path (sys.path mutation)
# was broken by the same shadowing; `_memex_module` uses importlib.util
# with a synthetic module name so the Memex package loads cleanly.


def _memex_agents_pkg():
    from scripts import backend_memex
    return backend_memex._memex_module("agents")


def _memex_stores():
    from scripts import backend_memex
    return backend_memex._memex_module("stores")


def _memex_db_path() -> str:
    from scripts import backend_memex
    return backend_memex._agents_db_path()


# ── Public CRUD surface ──────────────────────────────────────────────────


def create_agent(db_path: str, id: str, name: str, role_id: int,
                 profile: str) -> dict:
    """Create an `agents` row. In Local mode `db_path` is honored; in
    Memex mode it is ignored (the registered `agents` store path wins).
    """
    if mode_detector.detect_mode() == "memex":
        pkg = _memex_agents_pkg()
        return pkg.create_agent(_memex_db_path(), id, name, role_id, profile)
    now = _now()
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO agents (id, name, role_id, profile, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (id, name, role_id, profile, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_agent(db_path, id) or {}


def get_agent(db_path: str, agent_id: str) -> dict | None:
    if mode_detector.detect_mode() == "memex":
        pkg = _memex_agents_pkg()
        return pkg.get_agent(_memex_db_path(), agent_id)
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
    finally:
        conn.close()
    return dict(zip(cols, row))


def update_agent(db_path: str, agent_id: str, **kwargs) -> dict | None:
    """Update mutable fields (`name`, `role_id`, `profile`). Unknown
    keys are silently dropped — same shape as pre-retrofit behaviour."""
    allowed = {"name", "role_id", "profile"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if mode_detector.detect_mode() == "memex":
        pkg = _memex_agents_pkg()
        return pkg.update_agent(_memex_db_path(), agent_id, **fields)
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"UPDATE agents SET {set_clause} WHERE id = ?",
            (*fields.values(), agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_agent(db_path, agent_id)


def delete_agent(db_path: str, agent_id: str) -> bool:
    if mode_detector.detect_mode() == "memex":
        pkg = _memex_agents_pkg()
        if hasattr(pkg, "delete_agent"):
            return pkg.delete_agent(_memex_db_path(), agent_id)
        # Fallback for older Memex versions: drop straight to stores.
        return bool(_memex_stores().delete(
            name="agents", table="agents", row_id=agent_id))
    conn = get_connection(db_path)
    try:
        cur = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_agents(db_path: str, role_id: int | None = None) -> list[dict]:
    """List agents, optionally filtered by `role_id`. When `role_id` is
    None, returns all. Signature preserved from pre-retrofit."""
    if mode_detector.detect_mode() == "memex":
        pkg = _memex_agents_pkg()
        if role_id is not None and hasattr(pkg, "list_by_role"):
            return pkg.list_by_role(_memex_db_path(), role_id)
        all_agents = pkg.list_agents(_memex_db_path())
        if role_id is None:
            return all_agents
        return [a for a in all_agents if a.get("role_id") == role_id]
    conn = get_connection(db_path)
    try:
        if role_id is not None:
            cur = conn.execute(
                "SELECT * FROM agents WHERE role_id = ? ORDER BY name",
                (role_id,))
        else:
            cur = conn.execute("SELECT * FROM agents ORDER BY name")
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
    finally:
        conn.close()
    return [dict(zip(cols, r)) for r in rows]


def search_agents(db_path: str, query: str,
                  role_id: int | None = None) -> list[dict]:
    """Substring search on `name` + `profile`, optionally filtered by
    `role_id`. Memex mode reaches through `stores.query` because LIKE
    is not expressible via the equality-only `stores.query` where-dict."""
    pattern = f"%{query}%"
    if mode_detector.detect_mode() == "memex":
        stores = _memex_stores()
        if role_id is not None:
            return stores.query(
                "agents",
                "SELECT * FROM agents WHERE role_id = ? "
                "AND (name LIKE ? OR profile LIKE ?) ORDER BY name",
                (role_id, pattern, pattern),
            )
        return stores.query(
            "agents",
            "SELECT * FROM agents WHERE name LIKE ? OR profile LIKE ? "
            "ORDER BY name",
            (pattern, pattern),
        )
    conn = get_connection(db_path)
    try:
        if role_id is not None:
            cur = conn.execute(
                "SELECT * FROM agents WHERE role_id = ? "
                "AND (name LIKE ? OR profile LIKE ?) ORDER BY name",
                (role_id, pattern, pattern),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM agents WHERE name LIKE ? OR profile LIKE ? "
                "ORDER BY name",
                (pattern, pattern),
            )
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
    finally:
        conn.close()
    return [dict(zip(cols, r)) for r in rows]


# ── CLI shim ─────────────────────────────────────────────────────────────


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

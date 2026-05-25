"""Roles — wrapper around the backend facade for role CRUD.

The canonical create path goes through `backend.find_or_create_role`,
which is idempotent and routes per `mode_detector.detect_mode()` to
either `backend_local` (workspace-local `roles` table) or
`backend_memex` (Memex's `~/.memex/agents.db`). Reads + non-create
mutations (`get`, `update`, `delete`, `list`, `search`) reach the
appropriate backend directly because the facade does not expose
narrow-surface helpers for them — `_memex_core_query` / `memex_stores`
on the Memex side, `backend_local._conn()` on the Local side.

Public function signatures are preserved from pre-retrofit. The
`db_path` argument is retained for back-compat — Local mode discovers
the DB via `backend_local._local_db()` (workspace_root + .ai/atelier.db),
Memex mode resolves `~/.memex/agents.db` via the Memex registry.

`phase_bypasses.agent_id` no-FK semantics (design decision: spec §11
no-FK semantics for cross-store agent references — settled in review):
the v1.1.0 `phase_bypasses` row stores `agent_id TEXT NOT NULL` with
NO foreign key. Memex/Local agent stores are disjoint — Memex-mode
agents live in `~/.memex/agents.db` while the Atelier workspace DB
holds only soft references — so the audit row accepts stale or
otherwise-unresolvable agent IDs by design. Reintroduce the FK only
if/when both modes share an agents source. (Reseeded here because
`scripts/roles.py` is the canonical home of the role/agent identity
contract.)
"""

from __future__ import annotations

from datetime import datetime, timezone

from scripts import backend, mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_role(db_path: str, name: str, description: str) -> dict:
    """Create a role (or return the existing row if `name` is taken).

    Idempotent — routes through `backend.find_or_create_role`, which
    returns the existing row unchanged when `name` is already present
    (description is NOT updated on hit; updates go through
    `update_role`)."""
    return backend.find_or_create_role(name=name, description=description)


def get_role(db_path: str, role_id: int) -> dict | None:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        rows = backend_memex._memex_core_query(store="agents", table="roles", where={"id": role_id})
        return rows[0] if rows else None
    from scripts import backend_local

    c = backend_local._conn()
    try:
        row = c.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


def update_role(db_path: str, role_id: int, **kwargs) -> dict | None:
    """Update role fields. Allowed: `name`, `description`. If at least
    one allowed key is supplied, `updated_at` is refreshed alongside the
    update; if no allowed keys are supplied, the call is a no-op and the
    current row is returned without touching `updated_at`.

    In Memex mode, dispatches to `memex_stores.update` against the
    `agents` store (Memex's roles module does not expose a dedicated
    `update_role` helper). In Local mode, opens a fresh connection
    via `backend_local._conn()` and updates in-place."""
    allowed = {"name", "description"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        # No allowed keys supplied — avoid touching `updated_at` for a
        # payload-pollution-only update.
        return get_role(db_path, role_id)
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        updates["updated_at"] = _now()
        memex_stores = backend_memex._memex_module("stores")
        memex_stores.update(name="agents", table="roles", row_id=role_id, updates=updates)
        return get_role(db_path, role_id)
    from scripts import backend_local

    now = _now()
    c = backend_local._conn()
    try:
        if "name" in updates:
            c.execute(
                "UPDATE roles SET name = ?, updated_at = ? WHERE id = ?",
                (updates["name"], now, role_id),
            )
        if "description" in updates:
            c.execute(
                "UPDATE roles SET description = ?, updated_at = ? WHERE id = ?",
                (updates["description"], now, role_id),
            )
        c.commit()
    finally:
        c.close()
    return get_role(db_path, role_id)


def delete_role(db_path: str, role_id: int) -> bool:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        memex_stores = backend_memex._memex_module("stores")
        memex_stores.delete(name="agents", table="roles", row_id=role_id)
        return True
    from scripts import backend_local

    c = backend_local._conn()
    try:
        cur = c.execute("DELETE FROM roles WHERE id = ?", (role_id,))
        c.commit()
        deleted = cur.rowcount > 0
    finally:
        c.close()
    return deleted


def list_roles(db_path: str) -> list[dict]:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        memex_stores = backend_memex._memex_module("stores")
        return memex_stores.query("agents", "SELECT * FROM roles ORDER BY name", ())
    from scripts import backend_local

    c = backend_local._conn()
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM roles ORDER BY name").fetchall()]
    finally:
        c.close()
    return rows


def search_roles(db_path: str, query: str) -> list[dict]:
    """Substring search on name + description. Uses raw SQL via
    `memex_stores.query()` in Memex mode (SELECT-only, never commits)
    because the equality-only `where=` dict cannot express LIKE."""
    pattern = f"%{query}%"
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        memex_stores = backend_memex._memex_module("stores")
        return memex_stores.query(
            "agents",
            "SELECT * FROM roles WHERE name LIKE ? OR description LIKE ? ORDER BY name",
            (pattern, pattern),
        )
    from scripts import backend_local

    c = backend_local._conn()
    try:
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM roles WHERE name LIKE ? OR description LIKE ? ORDER BY name",
                (pattern, pattern),
            ).fetchall()
        ]
    finally:
        c.close()
    return rows


if __name__ == "__main__":
    import json
    import sys

    db_path = ".ai/atelier.db"
    cmd = sys.argv[1]

    if cmd == "create":
        print(json.dumps(create_role(db_path, name=sys.argv[2], description=sys.argv[3]), indent=2))
    elif cmd == "get":
        result = get_role(db_path, int(sys.argv[2]))
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "update":
        # Note: `--description ""` (empty string) clears the description
        # field (passed through to update_role as-is). Omit the flag
        # entirely to leave the field untouched.
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

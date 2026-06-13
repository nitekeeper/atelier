"""Agents — mode-conditional thin wrapper.

The canonical create path goes through `backend.find_or_create_agent`,
which is idempotent and routes per `mode_detector.detect_mode()` to
either `backend_local` (workspace-local `agents` table at
`<workspace_root>/.ai/atelier.db`) or `backend_memex` (Memex's
`~/.memex/agents.db`). Reads + non-create mutations (`get`, `update`,
`delete`, `list`, `search`) reach the appropriate backend directly
because the facade does not expose narrow-surface helpers for them —
the Memex agents package (via `_memex_module("agents")`, every call
wrapped in `backend_memex._memex_call_shim` so deferred call-time
`scripts.*` imports resolve) and the `backend_memex._memex_core_*`
primitives on the Memex side, `backend_local._conn()` on the Local
side. Never call `memex_stores.*` directly — see
`backend_memex._memex_core_raw_query`'s boundary note.

Public function signatures are preserved from pre-retrofit. The
`db_path` argument is retained for back-compat — Local mode discovers
the DB via `backend_local._conn()` (workspace_root + .ai/atelier.db),
Memex mode resolves `~/.memex/agents.db` via the Memex registry.
`scripts/db.py` is intentionally NOT imported here — Plan 3 deletes
that module and Local-mode callers shouldn't re-introduce the coupling
(see `backend_local.py` lines 16-20 for the policy).

Memex-mode dispatch uses `backend_memex._memex_module(...)` rather than
`from scripts import agents as ...`, because `sys.modules` caching
would otherwise resolve the latter back to THIS module (namespace
collision resolved in Wave 1 T8-10 via importlib.util).
"""

from __future__ import annotations

from datetime import datetime, timezone

from scripts import backend, mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Memex-mode primitives ────────────────────────────────────────────────


def _memex_agents_pkg():
    from scripts import backend_memex

    return backend_memex._memex_module("agents")


def _memex_db_path() -> str:
    from scripts import backend_memex

    return backend_memex._agents_db_path()


# ── Public CRUD surface ──────────────────────────────────────────────────


def create_agent(db_path: str, id: str, name: str, role_id: int, profile: str) -> dict:
    """Create an `agents` row (or return the existing row if `id` is
    taken). Idempotent — routes through `backend.find_or_create_agent`,
    which returns the existing row unchanged when `id` is already
    present (other fields are NOT updated on hit; updates go through
    `update_agent`).

    `db_path` is retained for signature compatibility only — Local mode
    resolves the DB via `backend_local._conn()` (workspace_root +
    .ai/atelier.db), Memex mode resolves `~/.memex/agents.db`.
    """
    return backend.find_or_create_agent(
        agent_id=id,
        name=name,
        role_id=role_id,
        profile=profile,
    )


def get_agent(db_path: str, agent_id: str) -> dict | None:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        pkg = _memex_agents_pkg()
        with backend_memex._memex_call_shim(pkg):
            return pkg.get_agent(_memex_db_path(), agent_id)
    from scripts import backend_local

    c = backend_local._conn()
    try:
        row = c.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


def update_agent(db_path: str, agent_id: str, **kwargs) -> dict | None:
    """Update mutable fields (`name`, `role_id`, `profile`). Unknown
    keys are silently dropped — same shape as pre-retrofit behaviour."""
    allowed = {"name", "role_id", "profile"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        pkg = _memex_agents_pkg()
        with backend_memex._memex_call_shim(pkg):
            return pkg.update_agent(_memex_db_path(), agent_id, **fields)
    from scripts import backend_local

    now = _now()
    c = backend_local._conn()
    try:
        # Per-column static SQL: each branch is a hardcoded literal so
        # the column name can't be poisoned by a future kwarg passthrough.
        if "name" in fields:
            c.execute(
                "UPDATE agents SET name = ?, updated_at = ? WHERE id = ?",
                (fields["name"], now, agent_id),
            )
        if "role_id" in fields:
            c.execute(
                "UPDATE agents SET role_id = ?, updated_at = ? WHERE id = ?",
                (fields["role_id"], now, agent_id),
            )
        if "profile" in fields:
            c.execute(
                "UPDATE agents SET profile = ?, updated_at = ? WHERE id = ?",
                (fields["profile"], now, agent_id),
            )
        c.commit()
    finally:
        c.close()
    return get_agent(db_path, agent_id)


def delete_agent(db_path: str, agent_id: str) -> bool:
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        pkg = _memex_agents_pkg()
        if hasattr(pkg, "delete_agent"):
            with backend_memex._memex_call_shim(pkg):
                return pkg.delete_agent(_memex_db_path(), agent_id)
        # Fallback for older Memex versions: drop to stores — through the
        # backend_memex facade so the delete runs under the Memex call
        # shim (deferred call-time imports; see
        # backend_memex._memex_core_raw_query's boundary note).
        # `_memex_core_delete` has no rowcount surface, so probe
        # existence first to preserve this function's bool contract.
        rows = backend_memex._memex_core_query(
            store="agents", table="agents", where={"id": agent_id}
        )
        if not rows:
            return False
        backend_memex._memex_core_delete(store="agents", table="agents", row_id=agent_id)
        return True
    from scripts import backend_local

    c = backend_local._conn()
    try:
        cur = c.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def list_agents(db_path: str, role_id: int | None = None) -> list[dict]:
    """List agents, optionally filtered by `role_id`. When `role_id` is
    None, returns all. Signature preserved from pre-retrofit."""
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        pkg = _memex_agents_pkg()
        with backend_memex._memex_call_shim(pkg):
            if role_id is not None and hasattr(pkg, "list_by_role"):
                return pkg.list_by_role(_memex_db_path(), role_id)
            all_agents = pkg.list_agents(_memex_db_path())
        if role_id is None:
            return all_agents
        return [a for a in all_agents if a.get("role_id") == role_id]
    from scripts import backend_local

    c = backend_local._conn()
    try:
        if role_id is not None:
            rows = c.execute(
                "SELECT * FROM agents WHERE role_id = ? ORDER BY name",
                (role_id,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM agents ORDER BY name").fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def search_agents(db_path: str, query: str, role_id: int | None = None) -> list[dict]:
    """Substring search on `name` + `profile`, optionally filtered by
    `role_id`. Memex mode reaches through
    `backend_memex._memex_core_raw_query` (SELECT-only, runs under the
    Memex call shim) because LIKE is not expressible via the
    equality-only where-dict."""
    pattern = f"%{query}%"
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        if role_id is not None:
            return backend_memex._memex_core_raw_query(
                store="agents",
                sql="SELECT * FROM agents WHERE role_id = ? "
                "AND (name LIKE ? OR profile LIKE ?) ORDER BY name",
                params=(role_id, pattern, pattern),
            )
        return backend_memex._memex_core_raw_query(
            store="agents",
            sql="SELECT * FROM agents WHERE name LIKE ? OR profile LIKE ? ORDER BY name",
            params=(pattern, pattern),
        )
    from scripts import backend_local

    c = backend_local._conn()
    try:
        if role_id is not None:
            rows = c.execute(
                "SELECT * FROM agents WHERE role_id = ? "
                "AND (name LIKE ? OR profile LIKE ?) ORDER BY name",
                (role_id, pattern, pattern),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM agents WHERE name LIKE ? OR profile LIKE ? ORDER BY name",
                (pattern, pattern),
            ).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


# ── CLI shim ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    import json
    import sys

    db_path = ".ai/atelier.db"
    cmd = sys.argv[1]

    if cmd == "create":
        print(
            json.dumps(
                create_agent(
                    db_path,
                    id=sys.argv[2],
                    name=sys.argv[3],
                    role_id=int(sys.argv[4]),
                    profile=sys.argv[5],
                ),
                indent=2,
            )
        )
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

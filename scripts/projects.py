# scripts/projects.py
"""Projects — wrapper around the backend facade for project-shaped rows.

v1.1.0 schema: `projects` has `workspace_id NOT NULL`, `slug`, `name`,
`description`, `phase`, `created_by`, `index_id`. **No `repo` column.**
The legacy `repo=` kwarg is still accepted (positional callers shouldn't
break) but is silently dropped — Memex mode keeps repo info on the
workspace identity, not the project row.

Writes route through `backend.write_project` (Plan 3 Task 2). Reads
that the facade doesn't yet expose (Plan 2 deferred `find_project`,
`list_projects`) use `backend_local._conn()` in Local mode or
`backend_memex._memex_core_query` in Memex mode — direct queries are
acceptable here because the facade explicitly defers these to v1.2.0
(spec §10) and Plan 3's other rewires use the same pattern.
"""

from __future__ import annotations

import re
import warnings
from datetime import datetime, timezone

from scripts import backend, mode_detector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(name: str) -> str:
    """Best-effort kebab-case slug. Mirrors `backend_local._slug` so a
    project created via this script is reachable by the same slug a
    direct backend caller would generate."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "untitled"


def _resolve_workspace_id() -> int:
    """Return the workspace id for the current process.

    v1.1.0 schema requires `workspace_id NOT NULL` on `projects`. The
    workspaces script + multi-workspace lookups land in v1.2.0; until
    then we resolve the singleton workspace row (or seed one in local
    mode if absent) so the write doesn't violate the FK.

    Mode-aware:
    - Local mode: opens `backend_local._conn()` and reads / seeds the
      singleton row in the local SQLite `workspaces` table.
    - Memex mode: queries the memex atelier store via
      `backend_memex._memex_core_query`. The atelier bootstrap (see
      `scripts/atelier_entrypoint.py`) provisions the singleton
      workspace row keyed on `_WORKSPACE_SLUG`, so no seeding is needed
      here; the regression for issue #6 bug #1 covers the "memex mode
      must not touch the local workspaces table" guarantee. The
      slug-less fallback below catches a bootstrap-race window where
      the workspace row exists but the slug is not yet what we expect
      (e.g. mid-rename); if both queries return empty we surface a
      `RuntimeError` rather than silently inserting.

    Note: in local mode this opens its own `_conn()` and `create_project`
    opens a second one inside `backend.write_project`. The double-open is
    intentional — WAL mode handles concurrent reads on the same DB
    fine, and folding the resolution into the facade is a v1.2.0 task
    (it requires the workspaces script to land first).
    """
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        # Single-workspace deployment (spec §6.7); identified by
        # `_WORKSPACE_SLUG`. Fall back to the first row if the slug is
        # absent for any reason — the workspaces table is bootstrapped
        # before any project create call lands here.
        rows = backend_memex._memex_core_query(
            store="atelier", table="workspaces", where={"slug": backend_memex._WORKSPACE_SLUG}
        )
        if not rows:
            rows = backend_memex._memex_core_query(store="atelier", table="workspaces")
        if not rows:
            raise RuntimeError(
                "memex atelier store has no workspace row; "
                "run atelier bootstrap before create_project"
            )
        return int(rows[0]["id"])

    from scripts import backend_local

    c = backend_local._conn()
    try:
        row = c.execute("SELECT id FROM workspaces ORDER BY id LIMIT 1").fetchone()
        if row is not None:
            return int(row["id"])
        # Seed a default workspace so the project insert can resolve its
        # FK. Matches the "singleton workspace until v1.2.0" pattern used
        # by other backend callers (see `backend_memex._WORKSPACE_SLUG`).
        now = _now()
        cur = c.execute(
            "INSERT INTO workspaces (slug, identity, name, description, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("default", "local:default", "Default", "Auto-seeded workspace", now, now),
        )
        c.commit()
        return int(cur.lastrowid)
    finally:
        c.close()


def create_project(
    db_path: str,
    name: str,
    description: str | None,
    created_by: str,
    repo: str | None = None,
    workspace_id: int | None = None,
    slug: str | None = None,
) -> dict:
    """Create a project row scoped to a workspace.

    `repo` is accepted for backwards-compat with pre-v1.1.0 callers but
    is silently dropped — v1.1.0 schema has no `repo` column. The
    `db_path` argument is also accepted for signature parity with the
    rest of `scripts/`; the backend resolves the active DB itself via
    `mode_detector` + workspace root, so the kwarg is unused.
    """
    del db_path  # silence linters — backend resolves the path itself
    del repo  # v1.1.0 has no `repo` column; kwarg kept for compat
    if slug is None:
        slug = _slug(name)
    if workspace_id is None:
        workspace_id = _resolve_workspace_id()
    result = backend.write_project(
        workspace_id=workspace_id,
        slug=slug,
        name=name,
        description=description,
        created_by=created_by,
    )
    # backend returns the full row + a `row_id` alias. Strip the alias
    # so callers see the canonical column names only.
    row = dict(result)
    row["id"] = row.pop("row_id", row.get("id"))
    return row


def get_project(db_path: str, project_id: int) -> dict | None:
    """Return the project row for `project_id` or None.

    `backend.find_project` is deferred to v1.2.0; until then we read
    directly from the active backend.
    """
    del db_path
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        rows = backend_memex._memex_core_query(
            store="atelier", table="projects", where={"id": project_id}
        )
        return rows[0] if rows else None
    from scripts import backend_local

    c = backend_local._conn()
    try:
        row = c.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


def update_project(
    db_path: str, project_id: int, *, agent_id: str = "system", **kwargs
) -> dict | None:
    """Update mutable project columns. `phase` routes through
    `backend.transition_phase`; other columns go direct.

    Allowed: `name`, `description`, `phase`. `agent_id` is passed
    through to `backend.transition_phase` for audit attribution
    (defaults to `"system"` for back-compat). Any other kwargs trigger
    a `DeprecationWarning` so the v1.1.0 migration window stays
    observable — pre-v1.1.0 callers updating `repo` will see a warning
    instead of a silent no-op.
    """
    del db_path
    allowed = {"name", "description", "phase"}
    extra = {k: v for k, v in kwargs.items() if k not in allowed}
    if extra:
        warnings.warn(
            f"unrecognized kwargs dropped: {sorted(extra)}",
            DeprecationWarning,
            stacklevel=2,
        )
    updates = {k: v for k, v in kwargs.items() if k in allowed}

    # Phase changes go through the facade so the audit-trail kwargs
    # (`agent_id`, `bypass_reason`) stay consistent across callers.
    if "phase" in updates:
        backend.transition_phase(
            project_id=project_id,
            to_phase=updates.pop("phase"),
            agent_id=agent_id,
        )

    if updates:
        if mode_detector.detect_mode() == "memex":
            from scripts import backend_memex

            updates["updated_at"] = _now()
            backend_memex._memex_core_update(
                store="atelier", table="projects", row_id=project_id, changes=updates
            )
        else:
            from scripts import backend_local

            now = _now()
            c = backend_local._conn()
            try:
                if "name" in updates:
                    c.execute(
                        "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
                        (updates["name"], now, project_id),
                    )
                if "description" in updates:
                    c.execute(
                        "UPDATE projects SET description = ?, updated_at = ? WHERE id = ?",
                        (updates["description"], now, project_id),
                    )
                c.commit()
            finally:
                c.close()

    return get_project(db_path="", project_id=project_id)


def delete_project(db_path: str, project_id: int) -> bool:
    """Hard-delete a project row. No facade method yet — Plan 3 keeps
    deletes as a direct backend operation."""
    del db_path
    # Memex Index rows are not row-deletable through the public facade;
    # soft-delete via metadata is the v1.2.0 path. For now, treat Memex
    # delete as a no-op that matches the old contract (returns False so
    # callers can tell it didn't happen).
    if mode_detector.detect_mode() == "memex":
        return False
    from scripts import backend_local

    c = backend_local._conn()
    try:
        cur = c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        c.commit()
    finally:
        c.close()
    return cur.rowcount > 0


def list_projects(db_path: str, phase: str | None = None) -> list[dict]:
    """Return every project row (optionally filtered by phase).

    `backend.list_projects` is deferred to v1.2.0; until then we read
    directly from the active backend, matching the pattern Plan 3's
    template uses for `get_project`.
    """
    del db_path
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        where: dict = {}
        if phase is not None:
            where["phase"] = phase
        rows = backend_memex._memex_core_query(
            store="atelier", table="projects", where=where or None
        )
        return list(rows)
    from scripts import backend_local

    c = backend_local._conn()
    try:
        if phase is not None:
            cur = c.execute(
                "SELECT * FROM projects WHERE phase = ? ORDER BY name",
                (phase,),
            )
        else:
            cur = c.execute("SELECT * FROM projects ORDER BY name")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        c.close()
    return rows


def search_projects(db_path: str, query: str) -> list[dict]:
    """Naive LIKE search over name / description.

    Local-mode only for now — the Memex backend exposes `find_documents`
    for FTS but not a structured projects search; spec §10 / v1.2.0
    folds this into `backend.list_projects(query=...)`.
    """
    del db_path
    # Memex backend exposes `find_documents` for FTS but not a structured
    # projects search; spec §10 / v1.2.0 folds this into
    # `backend.list_projects(query=...)`. Until then, Memex mode returns
    # an empty list rather than crash.
    if mode_detector.detect_mode() == "memex":
        return []
    from scripts import backend_local

    pattern = f"%{query}%"
    c = backend_local._conn()
    try:
        cur = c.execute(
            "SELECT * FROM projects WHERE name LIKE ? OR description LIKE ? ORDER BY name",
            (pattern, pattern),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        c.close()
    return rows


if __name__ == "__main__":
    import argparse
    import json
    import sys

    # v1.1.0 default — kept as a literal for signature parity with the
    # other CLI entrypoints, but `db_path` is ignored by every function
    # below (the backend resolves the active DB via `mode_detector` +
    # workspace root).
    db_path = ".ai/atelier.db"
    cmd = sys.argv[1]

    if cmd == "create":
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("description")
        parser.add_argument("created_by")
        parser.add_argument("--repo")  # accepted but ignored
        args = parser.parse_args(sys.argv[2:])
        print(
            json.dumps(
                create_project(
                    db_path,
                    name=args.name,
                    description=args.description,
                    created_by=args.created_by,
                    repo=args.repo,
                ),
                indent=2,
            )
        )
    elif cmd == "get":
        result = get_project(db_path, int(sys.argv[2]))
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "update":
        parser = argparse.ArgumentParser()
        parser.add_argument("project_id", type=int)
        parser.add_argument("--name")
        parser.add_argument("--description")
        parser.add_argument("--phase")
        parser.add_argument(
            "--agent",
            default="system",
            help="audit attribution for phase transitions (default: system)",
        )
        args = parser.parse_args(sys.argv[2:])
        kwargs = {
            k: v
            for k, v in vars(args).items()
            if k not in ("project_id", "agent") and v is not None
        }
        print(
            json.dumps(
                update_project(db_path, args.project_id, agent_id=args.agent, **kwargs), indent=2
            )
        )
    elif cmd == "delete":
        print("Deleted" if delete_project(db_path, int(sys.argv[2])) else "Not found")
    elif cmd == "list":
        parser = argparse.ArgumentParser()
        parser.add_argument("--phase")
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(list_projects(db_path, phase=args.phase), indent=2))
    elif cmd == "search":
        print(json.dumps(search_projects(db_path, sys.argv[2]), indent=2))

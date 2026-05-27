# scripts/projects.py
"""Projects — wrapper around the backend facade for project-shaped rows.

v1.1.0 schema: `projects` has `workspace_id NOT NULL`, `slug`, `name`,
`description`, `phase`, `created_by`, `index_id`. **No `repo` column.**
The legacy `repo=` kwarg is still accepted (positional callers shouldn't
break) but is silently dropped — Memex mode keeps repo info on the
workspace identity, not the project row.

Writes route through `backend.write_project` (Plan 3 Task 2). Reads
route through the facade methods landed in atelier#51 / #52 / #54:
`find_or_create_workspace`, `find_project`, `list_projects`,
`get_project` — no direct `backend_local._conn()` or
`backend_memex._memex_core_query` access here as of atelier#54.
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
    workspace registry is now reachable through the facade (atelier#51 +
    #54): we use `backend.list_workspaces()` to find the singleton row,
    seeding via `backend.find_or_create_workspace` in Local mode if the
    table is empty (Memex mode bootstraps its singleton row elsewhere).

    Singleton-workspace semantics here remain a backwards-compat
    convenience for callers that haven't yet been threaded through
    `scope.resolve_scope()`. Multi-workspace callers should resolve
    their workspace_id via `scope.resolve_scope()` and pass it
    explicitly rather than falling through to this helper.

    Memex mode: the atelier bootstrap (see `scripts/atelier_entrypoint.py`)
    provisions the singleton workspace row before any project create
    call lands here. If `list_workspaces()` returns empty in Memex mode,
    we surface a RuntimeError rather than silently inserting (Memex
    workspace creation goes through the atelier bootstrap, not this
    function).
    """
    workspaces = backend.list_workspaces()
    if workspaces:
        return int(workspaces[0]["id"])
    # Memex mode: bootstrap should have created the row; refuse to
    # paper over an unbootstrapped store.
    if mode_detector.detect_mode() == "memex":
        raise RuntimeError(
            "memex atelier store has no workspace row; run atelier bootstrap before create_project"
        )
    # Local mode: seed a default workspace so the project insert can
    # resolve its FK. Matches the "singleton workspace until atelier#55"
    # pattern. Routes through the facade for consistency with the rest
    # of the file.
    workspace = backend.find_or_create_workspace(
        identity="local:default",
        slug="default",
        name="Default",
        description="Auto-seeded workspace",
    )
    return int(workspace["id"])


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
    """Return the project row for `project_id` or None if absent.

    Routes through `backend.get_project(project_id)` (landed by
    atelier#54) — the lookup-by-id surface that parallels
    `backend.get_document`. The `(workspace_id, slug)` composite-key
    lookup lives in `backend.find_project`.
    """
    del db_path
    return backend.get_project(project_id=project_id)


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
    # soft-delete via metadata is a future path (no consumer today).
    # For now, treat Memex delete as a no-op that matches the old
    # contract (returns False so callers can tell it didn't happen).
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
    """Return every project row across all workspaces, optionally
    filtered by phase, ordered by name within each workspace then
    workspace-by-workspace (matches the historical single-workspace
    behavior — names sort naturally because there's one workspace today,
    and the multi-workspace ordering is acceptable until a real caller
    needs a global sort).

    Routes through `backend.list_workspaces()` + `backend.list_projects(
    workspace_id=...)` per workspace (landed by atelier#51 / #52),
    applying the optional phase filter in Python so the per-workspace
    `list_projects` calls don't need a phase parameter today.
    Cross-workspace listing is the spec §10.1 "iterate workspaces +
    call per-workspace" recipe documented on `backend.list_projects`.
    """
    del db_path
    rows: list[dict] = []
    for workspace in backend.list_workspaces():
        ws_projects = backend.list_projects(workspace_id=workspace["id"])
        if phase is not None:
            ws_projects = [p for p in ws_projects if p.get("phase") == phase]
        # Order by name within each workspace; the slug-ordered facade
        # list is re-sorted here because the public contract is
        # name-ordered for human display (predates atelier#52).
        ws_projects.sort(key=lambda p: p.get("name") or "")
        rows.extend(ws_projects)
    return rows


def search_projects(db_path: str, query: str) -> list[dict]:
    """Naive LIKE search over name / description.

    Local-mode only for now — the Memex backend exposes `find_documents`
    for document-text FTS but no structured projects-search surface
    today. A future `backend.list_projects(query=...)` extension is the
    likely shape; tracked as a follow-up when a real consumer needs it.
    """
    del db_path
    # No structured projects-search surface in Memex mode today; the
    # Memex backend has `find_documents` for document-text FTS but no
    # equivalent for projects. Return empty rather than crash; a follow-
    # up issue can land the surface when a consumer arrives.
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

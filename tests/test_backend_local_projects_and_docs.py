"""Local-mode project + document CRUD tests (atelier#52 / spec §10.1).

Covers the three stubs lifted from `_not_implemented` to real
implementations in this PR:

- `find_project(workspace_id, slug)` — composite-key lookup
- `list_projects(workspace_id)` — workspace-scoped listing, slug-ordered
- `get_document(doc_id)` — `project_documents` row by id

Memex-mode equivalents live in
`test_backend_memex_projects_and_docs.py`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import backend_local
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Stand up a fake workspace root with migrations applied and a
    seeded workspace row (so foreign-key constraints on projects /
    project_documents are satisfiable).

    Forces Local mode via `mode_detector.detect_mode` patch — without
    this, the facade tests (resolve_scope integration) would dispatch
    to Memex on dev machines where ~/.memex is installed and read
    cross-session state from there. Same hardening pattern as
    test_tasks.py's setup fixture.
    """
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    workspace = backend_local.find_or_create_workspace(
        identity="repo:test", slug="test", name="Test"
    )
    return {"root": root, "db": str(db), "workspace_id": workspace["id"]}


def _seed_project(db: str, *, workspace_id: int, slug: str, name: str | None = None) -> int:
    """Insert a project row directly; returns the new project_id."""
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (workspace_id, slug, name or slug, "", "design:open", "atelier-pm-1", now, now),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


# ── find_project ───────────────────────────────────────────────────────────


def test_find_project_returns_none_for_unknown_slug(workspace_root):
    assert (
        backend_local.find_project(workspace_id=workspace_root["workspace_id"], slug="nope") is None
    )


def test_find_project_returns_row_for_known_slug(workspace_root):
    pid = _seed_project(
        workspace_root["db"], workspace_id=workspace_root["workspace_id"], slug="auth"
    )
    row = backend_local.find_project(workspace_id=workspace_root["workspace_id"], slug="auth")
    assert row is not None
    assert row["id"] == pid
    assert row["slug"] == "auth"
    assert row["workspace_id"] == workspace_root["workspace_id"]


def test_find_project_is_workspace_scoped(workspace_root):
    """A project slug that exists in workspace A must NOT match a
    lookup in workspace B — spec §10.1 identity is `(workspace_id, slug)`,
    not just `slug`."""
    ws_a = workspace_root["workspace_id"]
    ws_b = backend_local.find_or_create_workspace(
        identity="repo:other", slug="other", name="Other"
    )["id"]
    _seed_project(workspace_root["db"], workspace_id=ws_a, slug="shared")
    # No project named "shared" exists in workspace B.
    assert backend_local.find_project(workspace_id=ws_b, slug="shared") is None


# ── list_projects ──────────────────────────────────────────────────────────


def test_list_projects_empty_returns_empty(workspace_root):
    assert backend_local.list_projects(workspace_id=workspace_root["workspace_id"]) == []


def test_list_projects_returns_workspace_rows_ordered_by_slug(workspace_root):
    ws = workspace_root["workspace_id"]
    _seed_project(workspace_root["db"], workspace_id=ws, slug="charlie")
    _seed_project(workspace_root["db"], workspace_id=ws, slug="alpha")
    _seed_project(workspace_root["db"], workspace_id=ws, slug="bravo")
    rows = backend_local.list_projects(workspace_id=ws)
    assert [r["slug"] for r in rows] == ["alpha", "bravo", "charlie"]


def test_list_projects_excludes_other_workspaces(workspace_root):
    """list_projects MUST be strictly workspace-scoped — no cross-
    workspace bleed even if the same slug exists in another workspace.
    """
    ws_a = workspace_root["workspace_id"]
    ws_b = backend_local.find_or_create_workspace(identity="repo:b", slug="b", name="B")["id"]
    _seed_project(workspace_root["db"], workspace_id=ws_a, slug="in-a")
    _seed_project(workspace_root["db"], workspace_id=ws_b, slug="in-b")
    rows_a = backend_local.list_projects(workspace_id=ws_a)
    rows_b = backend_local.list_projects(workspace_id=ws_b)
    assert [r["slug"] for r in rows_a] == ["in-a"]
    assert [r["slug"] for r in rows_b] == ["in-b"]


# ── get_document ───────────────────────────────────────────────────────────


def test_get_document_returns_none_for_unknown_id(workspace_root):
    assert backend_local.get_document(doc_id=999) is None


def test_get_document_returns_row_for_known_id(workspace_root):
    """Seed a project_documents row directly + verify get_document
    round-trips every column."""
    ws = workspace_root["workspace_id"]
    pid = _seed_project(workspace_root["db"], workspace_id=ws, slug="proj")
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(workspace_root["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO project_documents "
        "(workspace_id, project_id, domain, subdomain, title, filename, "
        "created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ws, pid, "design", "auth", "Auth Design", "docs/auth-design.md", "atelier-pm-1", now, now),
    )
    doc_id = cur.lastrowid
    conn.commit()
    conn.close()
    row = backend_local.get_document(doc_id=doc_id)
    assert row is not None
    assert row["id"] == doc_id
    assert row["workspace_id"] == ws
    assert row["project_id"] == pid
    assert row["domain"] == "design"
    assert row["subdomain"] == "auth"
    assert row["title"] == "Auth Design"
    assert row["filename"] == "docs/auth-design.md"


# ── Cross-function integration with resolve_scope ──────────────────────────


def test_resolve_scope_auto_selects_sole_project_end_to_end(workspace_root, monkeypatch, tmp_path):
    """With #50, #51, AND #52 landed, `resolve_scope` runs end-to-end
    against the real backend: workspace creates, sole project is
    auto-selected, slug pointer persists to state.json. No patches.
    """
    from scripts import scope

    # Redirect state.json into the test workspace so we don't touch
    # the real ~/.atelier on the dev machine.
    state_target = tmp_path / "state.json"
    monkeypatch.setattr(scope, "_state_path", lambda: state_target)

    ws_id = workspace_root["workspace_id"]
    _seed_project(workspace_root["db"], workspace_id=ws_id, slug="sole-project")

    s = scope.resolve_scope(workspace_override="repo:test")
    assert s.workspace is not None
    assert s.workspace["id"] == ws_id
    assert s.project is not None
    assert s.project["slug"] == "sole-project"
    # State.json received the slug pointer.
    state = scope.read_session_state()
    assert state["workspaces"][str(ws_id)]["current_project_slug"] == "sole-project"

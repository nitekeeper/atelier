"""Call-site activation tests (atelier#54).

Verifies that the deferred call sites in `scripts/projects.py` and
`scripts/documents.py` now route through the real backend facade
instead of dropping into `backend_local._conn()` /
`backend_memex._memex_core_query` directly. The 4 sites listed in
atelier#54:

- `projects.get_project(db_path, project_id)` → `backend.get_project`
- `projects.list_projects(db_path, phase=None)` →
  `backend.list_workspaces` + `backend.list_projects` iteration
- `projects._resolve_workspace_id()` → `backend.list_workspaces` +
  `backend.find_or_create_workspace`
- `documents.get_document(db_path, doc_id)` → `backend.get_document`

Also tests the new `backend.get_project(project_id)` facade method
that atelier#54 added to fill the lookup-by-id gap (paralleling
`get_document(doc_id)`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import backend, backend_local, backend_memex
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Local-mode workspace with migrations applied + a singleton
    workspace row seeded."""
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
    ws = backend_local.find_or_create_workspace(identity="repo:test", slug="test", name="Test")
    return {"root": root, "db": str(db), "workspace_id": ws["id"]}


def _seed_project(db: str, *, workspace_id: int, slug: str, name: str, phase: str) -> int:
    """Insert a project row directly; returns the new project_id."""
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            slug,
            name,
            "",
            phase,
            "atelier-pm-1",
            "2026-05-26T12:00Z",
            "2026-05-26T12:00Z",
        ),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


# ── backend.get_project (new facade method, atelier#54) ────────────────────


def test_backend_get_project_returns_row_when_present(workspace_root):
    pid = _seed_project(
        workspace_root["db"],
        workspace_id=workspace_root["workspace_id"],
        slug="auth",
        name="Auth",
        phase="design:open",
    )
    row = backend.get_project(project_id=pid)
    assert row is not None
    assert row["id"] == pid
    assert row["slug"] == "auth"


def test_backend_get_project_returns_none_for_unknown_id(workspace_root):
    assert backend.get_project(project_id=999) is None


def test_backend_get_project_memex_routes_through_facade(monkeypatch):
    """Memex-mode dispatch hits `backend_memex.get_project`."""
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    with patch.object(backend_memex, "get_project") as memex_get:
        memex_get.return_value = {"id": 1, "slug": "memexed"}
        result = backend.get_project(project_id=1)
        memex_get.assert_called_once_with(project_id=1)
        assert result == {"id": 1, "slug": "memexed"}


# ── projects.get_project wired to backend.get_project ──────────────────────


def test_projects_get_project_routes_through_facade(workspace_root, monkeypatch):
    """The wrapper at `scripts.projects.get_project` MUST call
    `backend.get_project(project_id=...)` — not the old direct
    `_conn()`/`_memex_core_query` access."""
    from scripts import projects

    pid = _seed_project(
        workspace_root["db"],
        workspace_id=workspace_root["workspace_id"],
        slug="proj",
        name="P",
        phase="design:open",
    )
    # Spy on the facade method to verify the call landed there.
    with patch.object(backend, "get_project", wraps=backend.get_project) as spy:
        row = projects.get_project("/unused", pid)
        spy.assert_called_once_with(project_id=pid)
    assert row is not None
    assert row["id"] == pid


def test_projects_get_project_returns_none_for_missing_id(workspace_root):
    from scripts import projects

    assert projects.get_project("/unused", 999) is None


# ── projects.list_projects iterates workspaces via facade ──────────────────


def test_projects_list_projects_iterates_workspaces_via_facade(workspace_root):
    """`projects.list_projects` MUST call `backend.list_workspaces()`
    + `backend.list_projects(workspace_id=...)` per workspace — the
    new spec §10.1-compliant iteration recipe."""
    from scripts import projects

    ws_a = workspace_root["workspace_id"]
    ws_b = backend_local.find_or_create_workspace(
        identity="repo:second", slug="second", name="Second"
    )["id"]
    _seed_project(
        workspace_root["db"],
        workspace_id=ws_a,
        slug="proj-in-a",
        name="A-proj",
        phase="design:open",
    )
    _seed_project(
        workspace_root["db"],
        workspace_id=ws_b,
        slug="proj-in-b",
        name="B-proj",
        phase="design:open",
    )
    # No phase filter → returns BOTH projects across BOTH workspaces.
    rows = projects.list_projects("/unused")
    slugs = sorted(r["slug"] for r in rows)
    assert slugs == ["proj-in-a", "proj-in-b"]


def test_projects_list_projects_applies_phase_filter_in_python(workspace_root):
    """The phase filter runs in Python after the facade-iteration
    fetches all workspace projects. Verify the filter actually narrows
    the result set."""
    from scripts import projects

    ws_a = workspace_root["workspace_id"]
    _seed_project(
        workspace_root["db"], workspace_id=ws_a, slug="opened", name="Opened", phase="design:open"
    )
    _seed_project(
        workspace_root["db"],
        workspace_id=ws_a,
        slug="shipped",
        name="Shipped",
        phase="ship:complete",
    )
    rows = projects.list_projects("/unused", phase="design:open")
    assert len(rows) == 1
    assert rows[0]["slug"] == "opened"


def test_projects_list_projects_calls_facade_methods(workspace_root, monkeypatch):
    """Confirm the iteration uses the facade methods, not the direct
    backend access patterns the deferral comments referenced."""
    from scripts import projects

    with (
        patch.object(backend, "list_workspaces", wraps=backend.list_workspaces) as lw_spy,
        patch.object(backend, "list_projects", wraps=backend.list_projects) as lp_spy,
    ):
        projects.list_projects("/unused")
        lw_spy.assert_called_once_with()
        # One list_projects call per workspace (1 today).
        assert lp_spy.call_count == 1


# ── projects._resolve_workspace_id routes through facade ───────────────────


def test_resolve_workspace_id_uses_existing_singleton(workspace_root):
    """When a workspace row already exists, `_resolve_workspace_id`
    returns its id via `backend.list_workspaces()` — no seeding."""
    from scripts import projects

    ws_id = projects._resolve_workspace_id()
    assert ws_id == workspace_root["workspace_id"]


def test_resolve_workspace_id_seeds_via_facade_when_empty(tmp_path, monkeypatch):
    """When the workspaces table is empty AND mode is Local,
    `_resolve_workspace_id` seeds a default workspace via
    `backend.find_or_create_workspace` (not raw INSERT)."""
    from scripts import mode_detector, projects

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "empty-repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    # No workspace seeded.
    with patch.object(
        backend, "find_or_create_workspace", wraps=backend.find_or_create_workspace
    ) as foc_spy:
        ws_id = projects._resolve_workspace_id()
        foc_spy.assert_called_once()
        assert foc_spy.call_args.kwargs["slug"] == "default"
    assert isinstance(ws_id, int)
    assert ws_id >= 1


def test_resolve_workspace_id_memex_raises_when_unbootstrapped(monkeypatch):
    """Memex mode must NOT silently seed — bootstrap is the canonical
    Memex workspace-creation path. Empty Memex store → RuntimeError."""
    from scripts import mode_detector, projects

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    with patch.object(backend, "list_workspaces", return_value=[]):
        with pytest.raises(RuntimeError, match="bootstrap"):
            projects._resolve_workspace_id()


# ── documents.get_document wired to backend.get_document ───────────────────


def test_documents_get_document_routes_through_facade(workspace_root, monkeypatch):
    """`scripts.documents.get_document` MUST call
    `backend.get_document(doc_id=...)` — not raw `_conn()` or
    `_memex_core_query` access."""
    from scripts import documents

    # Seed a document row directly.
    pid = _seed_project(
        workspace_root["db"],
        workspace_id=workspace_root["workspace_id"],
        slug="proj",
        name="P",
        phase="design:open",
    )
    conn = sqlite3.connect(workspace_root["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO project_documents "
        "(workspace_id, project_id, domain, subdomain, title, filename, "
        "created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_root["workspace_id"],
            pid,
            "design",
            "auth",
            "Auth Design",
            "docs/auth.md",
            "atelier-pm-1",
            "2026-05-26T12:00Z",
            "2026-05-26T12:00Z",
        ),
    )
    doc_id = cur.lastrowid
    conn.commit()
    conn.close()
    with patch.object(backend, "get_document", wraps=backend.get_document) as spy:
        row = documents.get_document("/unused", doc_id)
        spy.assert_called_once_with(doc_id=doc_id)
    assert row is not None
    # The legacy adapter preserves `type` field — verify the adapter
    # still fires after the facade-routing change.
    assert "type" in row


def test_documents_get_document_returns_none_for_missing(workspace_root):
    from scripts import documents

    assert documents.get_document("/unused", 999) is None


# ── Anti-regression: no v1.2 / deferred comments survive ───────────────────


def test_no_v1_2_deferral_comments_in_target_files():
    """Acceptance-criterion guard for atelier#54: `scripts/projects.py`
    and `scripts/documents.py` MUST NOT contain any `v1.2` or `deferred`
    references after this PR lands. Anti-regression for the comment
    cleanup; protects against future accidental reintroduction."""
    for fname in ("scripts/projects.py", "scripts/documents.py"):
        text = (Path(__file__).parent.parent / fname).read_text(encoding="utf-8")
        assert "v1.2" not in text, f"{fname} still contains a v1.2 reference"
        assert "deferred" not in text.lower(), f"{fname} still contains a 'deferred' reference"

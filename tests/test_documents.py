# tests/test_documents.py
"""Tests for `scripts/documents.py` after the Plan 3 Task 1 backend-facade
rewire. The module's public surface is unchanged from v1.0.13; the
internals now call `scripts.backend` instead of opening SQLite directly.

Fixtures stand up an isolated workspace per spec §10.2 (cwd-rooted +
`.git` marker) so `workspace_root()` resolves under `tmp_path`. We seed
workspaces / roles / agents / projects via direct SQL because Plan 3
Tasks 2/7/8 (rewires for projects, roles, agents) haven't landed yet —
the per-task plan dispatches all 9 in parallel and they merge as a
batch. Once those siblings land, the seed helper can switch to calling
`scripts.projects.create_project` etc.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.documents import (
    create_document,
    delete_document,
    get_document,
    list_documents,
    search_documents,
    update_document,
)
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed_minimum(db: str) -> tuple[int, int, str]:
    """Insert one workspace + role + agent + project. Returns the ids.

    Mirrors the seed helper in `tests/test_backend_local_documents.py`
    so the two test files share the v1.1.0 schema-aware fixture pattern.
    """
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        cur = conn.execute(
            "INSERT INTO workspaces (slug, identity, name, description, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("test-ws", "repo:test-ws", "Test", "test ws", now, now),
        )
        ws_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("pm", "PM", now, now),
        )
        role_id = cur.lastrowid
        agent_id = "pm-1"
        conn.execute(
            "INSERT INTO agents (id, name, role_id, profile, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, "PM", role_id, "Expert", now, now),
        )
        cur = conn.execute(
            "INSERT INTO projects (workspace_id, slug, name, description, "
            "phase, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ws_id, "auth", "Auth Service", "OAuth2", "design:open", agent_id, now, now),
        )
        project_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return ws_id, project_id, agent_id


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolated workspace under `tmp_path` with `.git`, migrated atelier.db,
    and a seeded project. chdir's into the workspace root so
    `workspace_root()` resolves there for the duration of the test.

    Forces `mode_detector.detect_mode` to return "local" so the rewired
    `scripts.documents` routes through `backend_local` regardless of
    whether the developer has Memex installed at `~/.memex/`. This
    mirrors the pattern in `test_backend_local_documents.py` (no Memex
    invocation in the suite path).
    """
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    ws_id, project_id, agent_id = _seed_minimum(str(db))
    return {
        "root": root,
        "db": str(db),
        "workspace_id": ws_id,
        "project_id": project_id,
        "agent_id": agent_id,
    }


def _make_file(root: Path, rel: str, body: str = "# placeholder\n") -> None:
    """Create the on-disk markdown file the rewired `create_document` expects.

    Plan 1 Task 5 Step 9's note: v1.0.13's tests passed filenames that
    didn't exist on disk because the old implementation never read the
    file. The new path indexes the body, so the file MUST exist; we
    create it eagerly here.
    """
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")


@pytest.fixture
def db_path(workspace):
    """Back-compat alias matching the old fixture name. `db_path` is now
    just the local-mode atelier.db that `backend_local` resolves on its
    own; the value is still threaded through `documents.*` calls for
    signature parity.
    """
    return workspace["db"]


@pytest.fixture
def project_id(workspace):
    return workspace["project_id"]


@pytest.fixture
def agent_id(workspace):
    return workspace["agent_id"]


def test_create_document(workspace, db_path, project_id, agent_id):
    _make_file(workspace["root"], "design/DESIGN.md", "# Auth Design\n")
    doc = create_document(
        db_path,
        project_id=project_id,
        type="design",
        title="Auth Design Doc",
        filename="design/DESIGN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    assert doc["id"] == 1
    assert doc["project_id"] == project_id
    assert doc["type"] == "design"
    assert doc["filename"] == "design/DESIGN.md"


def test_get_document(workspace, db_path, project_id, agent_id):
    _make_file(workspace["root"], "DESIGN.md", "# Auth Design\n")
    create_document(
        db_path,
        project_id=project_id,
        type="design",
        title="Auth Design",
        filename="DESIGN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    doc = get_document(db_path, 1)
    assert doc["title"] == "Auth Design"


def test_get_document_missing_returns_none(workspace, db_path):
    assert get_document(db_path, 999) is None


def test_update_document(workspace, db_path, project_id, agent_id):
    _make_file(workspace["root"], "DESIGN.md", "# Auth Design\n")
    create_document(
        db_path,
        project_id=project_id,
        type="design",
        title="Old Title",
        filename="DESIGN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    updated = update_document(db_path, 1, title="New Title")
    assert updated["title"] == "New Title"


def test_delete_document(workspace, db_path, project_id, agent_id):
    _make_file(workspace["root"], "DESIGN.md", "# Auth Design\n")
    create_document(
        db_path,
        project_id=project_id,
        type="design",
        title="Auth Design",
        filename="DESIGN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    assert delete_document(db_path, 1) is True
    assert get_document(db_path, 1) is None


def test_list_documents_by_project(workspace, db_path, project_id, agent_id):
    _make_file(workspace["root"], "DESIGN.md", "# d\n")
    _make_file(workspace["root"], "PLAN.md", "# p\n")
    create_document(
        db_path,
        project_id=project_id,
        type="design",
        title="Design Doc",
        filename="DESIGN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    create_document(
        db_path,
        project_id=project_id,
        type="implementation-plan",
        title="Impl Plan",
        filename="PLAN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    docs = list_documents(db_path, project_id=project_id)
    assert len(docs) == 2


def test_list_documents_by_type(workspace, db_path, project_id, agent_id):
    _make_file(workspace["root"], "DESIGN.md", "# d\n")
    _make_file(workspace["root"], "PLAN.md", "# p\n")
    create_document(
        db_path,
        project_id=project_id,
        type="design",
        title="Design Doc",
        filename="DESIGN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    create_document(
        db_path,
        project_id=project_id,
        type="implementation-plan",
        title="Impl Plan",
        filename="PLAN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    docs = list_documents(db_path, type="design")
    assert len(docs) == 1
    assert docs[0]["type"] == "design"


def test_search_documents(workspace, db_path, project_id, agent_id):
    _make_file(workspace["root"], "DESIGN.md", "# OAuth2 design\n")
    _make_file(workspace["root"], "PLAN.md", "# stripe plan\n")
    create_document(
        db_path,
        project_id=project_id,
        type="design",
        title="OAuth2 Design",
        filename="DESIGN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    create_document(
        db_path,
        project_id=project_id,
        type="implementation-plan",
        title="Stripe Plan",
        filename="PLAN.md",
        created_by=agent_id,
        workspace_id=workspace["workspace_id"],
    )
    results = search_documents(db_path, query="OAuth")
    assert len(results) == 1
    assert results[0]["title"] == "OAuth2 Design"

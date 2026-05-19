"""Plan 2 Task 5 — Local-mode document writes.

Tests `backend_local.write_document` / `write_task` / `write_meeting` /
`write_project` against the v1.1.0 schema (workspace_id NOT NULL on
project_documents/tasks/meeting_minutes; tasks + meetings live in their
own tables, NOT in project_documents).
"""

from __future__ import annotations
import sqlite3
from pathlib import Path

import pytest

from scripts import backend_local
from scripts.migrate import apply_migrations


MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed_minimum(db_path: str) -> tuple[int, int]:
    """Seed workspaces + roles + agents + projects for the v1.1.0 schema.

    Returns (workspace_id, project_id). The v1.1.0 schema requires
    workspace_id NOT NULL on most tables, so every test needs a workspace
    row and a project row to anchor writes to.
    """
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("myproj", "repo:myproj", "MyProj", "test workspace", now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("Product Manager", "PM", now, now),
    )
    role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("atelier-pm-1", "PM", role_id, "pm", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "auth", "Auth Service", "OAuth2 service", "design:open", "atelier-pm-1", now, now),
    )
    proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return ws_id, proj_id


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Stand up a fake workspace root with .ai/atelier.db migrated."""
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    ws_id, proj_id = _seed_minimum(str(db))
    return {"root": root, "db": str(db), "workspace_id": ws_id, "project_id": proj_id}


# ── write_document ─────────────────────────────────────────────────────────


def test_write_document_creates_local_row(workspace):
    r = backend_local.write_document(
        workspace_id=workspace["workspace_id"],
        project_id=workspace["project_id"],
        domain="design",
        subdomain="auth",
        title="Auth Design",
        body="# Auth\n\nOAuth2 flow.",
        metadata={},
        caller_agent_id="atelier-pm-1",
    )
    assert r["row_id"] >= 1
    # Verify row landed in project_documents (NOT a separate documents table).
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM project_documents WHERE id = ?", (r["row_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["title"] == "Auth Design"
    assert row["domain"] == "design"
    assert row["subdomain"] == "auth"
    assert row["workspace_id"] == workspace["workspace_id"]
    assert row["project_id"] == workspace["project_id"]


def test_write_document_archives_raw_body(workspace):
    r = backend_local.write_document(
        workspace_id=workspace["workspace_id"],
        project_id=workspace["project_id"],
        domain="design",
        subdomain=None,
        title="X",
        body="hello world",
        metadata={},
        caller_agent_id="atelier-pm-1",
    )
    # Raw body must be archived under <workspace_root>/.ai/raw/.
    # The archive shards on the first two hex chars of the content hash to
    # keep the directory small; recurse with rglob to find the file.
    raw_dir = workspace["root"] / ".ai" / "raw"
    assert raw_dir.is_dir()
    raw_files = list(raw_dir.rglob("*.md"))
    assert len(raw_files) == 1
    assert "hello world" in raw_files[0].read_text(encoding="utf-8")
    # Row should reference the raw file via filename (relative to workspace_root).
    assert r["row_id"] >= 1


# ── write_task ─────────────────────────────────────────────────────────────


def test_write_task_creates_task_row(workspace):
    r = backend_local.write_task(
        workspace_id=workspace["workspace_id"],
        project_id=workspace["project_id"],
        title="Fix bug",
        description="OAuth 500",
        subdomain="bug",
        created_by="atelier-pm-1",
    )
    assert r["row_id"] >= 1
    # Verify it landed in tasks (NOT project_documents).
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (r["row_id"],)).fetchone()
    # Confirm nothing was written to project_documents for the task.
    pd = conn.execute("SELECT COUNT(*) FROM project_documents").fetchone()[0]
    conn.close()
    assert row is not None
    assert row["title"] == "Fix bug"
    assert row["subdomain"] == "bug"
    assert row["status"] == "pending"
    assert row["project_id"] == workspace["project_id"]
    assert pd == 0


# ── write_meeting ──────────────────────────────────────────────────────────


def test_write_meeting_writes_minutes_markdown(workspace):
    r = backend_local.write_meeting(
        workspace_id=workspace["workspace_id"],
        project_id=workspace["project_id"],
        title="Kickoff",
        date="2026-05-16",
        summary="scope",
        decisions="oauth2",
        subdomain="design-review",
        created_by="atelier-pm-1",
    )
    assert r["row_id"] >= 1
    # On-disk markdown file at .ai/meetings/<date>-<slug>.md.
    meetings_dir = workspace["root"] / ".ai" / "meetings"
    meetings = list(meetings_dir.glob("*.md"))
    assert len(meetings) == 1
    body = meetings[0].read_text(encoding="utf-8")
    assert "Kickoff" in body
    assert "oauth2" in body
    # DB row landed in meeting_minutes.
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM meeting_minutes WHERE id = ?", (r["row_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["title"] == "Kickoff"
    assert row["date"] == "2026-05-16"
    assert row["subdomain"] == "design-review"


# ── write_project ──────────────────────────────────────────────────────────


def test_write_project_creates_project_row(workspace):
    r = backend_local.write_project(
        workspace_id=workspace["workspace_id"],
        slug="payments",
        name="Payments Service",
        description="Stripe + ACH integration.",
        created_by="atelier-pm-1",
    )
    assert r["row_id"] >= 1
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (r["row_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["slug"] == "payments"
    assert row["name"] == "Payments Service"
    assert row["workspace_id"] == workspace["workspace_id"]
    assert row["phase"] == "design:open"

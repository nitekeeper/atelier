"""Plan 2 Task 7 — Local-mode reads + cross-plan helpers.

Tests `backend_local.find_documents` / `get_task` / `list_tasks` /
`lookup_index_id_by_source_ref` / `find_or_create_role` /
`find_or_create_agent` against the v1.1.0 schema.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

import pytest

from scripts import backend_local
from scripts.migrate import apply_migrations


MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed_with_docs_and_tasks(db_path: str) -> dict:
    """Seed workspaces, a project, and some documents + tasks for read tests."""
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    # Two workspaces — so workspace_id filtering can be tested.
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("ws-a", "repo:ws-a", "WS A", None, now, now),
    )
    ws_a = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("ws-b", "repo:ws-b", "WS B", None, now, now),
    )
    ws_b = cur.lastrowid
    # PM role + agent (we use the seeded local roles/agents tables).
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("Product Manager", "PM", now, now),
    )
    pm_role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("atelier-pm-1", "PM", pm_role_id, "pm", now, now),
    )
    # Two projects in ws_a.
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_a, "auth", "Auth", "d", "design:open",
         "atelier-pm-1", now, now),
    )
    proj_a1 = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_a, "billing", "Billing", "d", "design:open",
         "atelier-pm-1", now, now),
    )
    proj_a2 = cur.lastrowid
    # One project in ws_b.
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_b, "ops", "Ops", "d", "design:open",
         "atelier-pm-1", now, now),
    )
    proj_b1 = cur.lastrowid
    # Mix of documents across workspaces / projects / domains / subdomains.
    docs = [
        # (workspace_id, project_id, domain, subdomain, title, filename)
        (ws_a, proj_a1, "design", "auth", "Auth Design", "design/auth.md"),
        (ws_a, proj_a1, "design", "auth", "Auth Redesign", "design/auth2.md"),
        (ws_a, proj_a1, "adr", "auth", "ADR-001", "adr/001.md"),
        (ws_a, proj_a2, "design", "billing", "Billing Design", "design/billing.md"),
        (ws_b, proj_b1, "design", "ops", "Ops Design", "design/ops.md"),
    ]
    for ws, pr, dom, sub, tit, fn in docs:
        conn.execute(
            "INSERT INTO project_documents (workspace_id, project_id, "
            "domain, subdomain, title, filename, created_by, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ws, pr, dom, sub, tit, fn, "atelier-pm-1", now, now),
        )
    # Tasks for proj_a1.
    cur = conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (proj_a1, "Fix login bug", "500", "pending", "atelier-pm-1", now, now),
    )
    task_pending = cur.lastrowid
    conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (proj_a1, "Ship oauth", "done", "complete", "atelier-pm-1", now, now),
    )
    conn.commit()
    conn.close()
    return {
        "workspace_a": ws_a, "workspace_b": ws_b,
        "project_a1": proj_a1, "project_a2": proj_a2,
        "project_b1": proj_b1,
        "task_pending": task_pending,
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    ids = _seed_with_docs_and_tasks(str(db))
    return {"root": root, "db": str(db), **ids}


# ── find_documents (filter-based reads, FTS5 deferred) ─────────────────────

def test_find_documents_filters_by_workspace_id(workspace):
    a = backend_local.find_documents(
        query="design", workspace_id=workspace["workspace_a"])
    b = backend_local.find_documents(
        query="design", workspace_id=workspace["workspace_b"])
    titles_a = {d["title"] for d in a}
    titles_b = {d["title"] for d in b}
    assert "Ops Design" not in titles_a
    assert "Ops Design" in titles_b
    assert "Auth Design" in titles_a


def test_find_documents_filters_by_project_id(workspace):
    r = backend_local.find_documents(
        query="design", project_id=workspace["project_a1"])
    titles = {d["title"] for d in r}
    assert "Auth Design" in titles
    assert "Billing Design" not in titles


def test_find_documents_filters_by_domain(workspace):
    """Domain filter narrows to ADR rows. Empty query disables text match
    so this tests the structured filter in isolation."""
    r = backend_local.find_documents(query="", domain="adr")
    titles = {d["title"] for d in r}
    assert titles == {"ADR-001"}


def test_find_documents_filters_by_subdomain(workspace):
    r = backend_local.find_documents(query="design", subdomain="billing")
    titles = {d["title"] for d in r}
    assert titles == {"Billing Design"}


def test_find_documents_limit_applied(workspace):
    """Limit caps the result set even when the filter matches more rows."""
    # The seed has 4 design-domain rows across both workspaces; limit=2
    # must trim the response.
    r = backend_local.find_documents(query="", domain="design", limit=2)
    assert len(r) == 2


# ── get_task ───────────────────────────────────────────────────────────────

def test_get_task_returns_row(workspace):
    r = backend_local.get_task(task_id=workspace["task_pending"])
    assert r is not None
    assert r["title"] == "Fix login bug"
    assert r["status"] == "pending"


def test_get_task_returns_none_when_missing(workspace):
    assert backend_local.get_task(task_id=99999) is None


# ── list_tasks ─────────────────────────────────────────────────────────────

def test_list_tasks_filters_by_status(workspace):
    pending = backend_local.list_tasks(
        project_id=workspace["project_a1"], status="pending")
    complete = backend_local.list_tasks(
        project_id=workspace["project_a1"], status="complete")
    all_tasks = backend_local.list_tasks(project_id=workspace["project_a1"])
    assert len(pending) == 1
    assert len(complete) == 1
    assert len(all_tasks) == 2
    assert pending[0]["title"] == "Fix login bug"


# ── lookup_index_id_by_source_ref ──────────────────────────────────────────

def test_lookup_index_id_by_source_ref_returns_none_in_local_mode(workspace):
    """Local mode has no federated Memex Index — there is no `index_id` to
    look up by `source_ref`. The method exists for facade-signature parity
    with the Memex backend; in Local mode it always returns None.
    """
    assert backend_local.lookup_index_id_by_source_ref(
        source_ref="atelier:tasks:1") is None
    assert backend_local.lookup_index_id_by_source_ref(
        source_ref="atelier:project_documents:42") is None


# ── find_or_create_role ────────────────────────────────────────────────────

def test_find_or_create_role_idempotent(workspace):
    first = backend_local.find_or_create_role(
        name="Designer", description="UI/UX")
    second = backend_local.find_or_create_role(
        name="Designer", description="ignored on hit")
    assert first["id"] == second["id"]
    assert second["description"] == "UI/UX"  # unchanged on hit
    # Confirm no duplicate row.
    conn = sqlite3.connect(workspace["db"])
    count = conn.execute(
        "SELECT COUNT(*) FROM roles WHERE name = 'Designer'"
    ).fetchone()[0]
    conn.close()
    assert count == 1


# ── find_or_create_agent ───────────────────────────────────────────────────

def test_find_or_create_agent_idempotent(workspace):
    role = backend_local.find_or_create_role(
        name="Engineer", description="Eng")
    a = backend_local.find_or_create_agent(
        agent_id="atelier-eng-1", name="Eng 1",
        role_id=role["id"], profile="engineer")
    b = backend_local.find_or_create_agent(
        agent_id="atelier-eng-1", name="ignored",
        role_id=role["id"], profile="ignored")
    assert a["id"] == b["id"]
    assert b["name"] == "Eng 1"  # unchanged on hit
    conn = sqlite3.connect(workspace["db"])
    count = conn.execute(
        "SELECT COUNT(*) FROM agents WHERE id = 'atelier-eng-1'"
    ).fetchone()[0]
    conn.close()
    assert count == 1

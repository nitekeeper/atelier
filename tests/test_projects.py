# tests/test_projects.py
"""Projects script — Plan 3 Task 2 rewire.

v1.1.0 schema requires `workspace_id NOT NULL` on `projects` and drops
the legacy `repo` column. The script now routes through `backend.write_project`,
which resolves the DB via `backend_local._workspace_root()` (CWD-based)
rather than the `db_path` kwarg, so these tests stand up a fake workspace
root under `tmp_path` and `monkeypatch.chdir()` into it. The `db_path`
positional kwarg is kept on the public API for signature parity but is
ignored by the backend.
"""
from __future__ import annotations
from pathlib import Path

import pytest

from scripts.migrate import apply_migrations
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.projects import (
    create_project,
    get_project,
    update_project,
    delete_project,
    list_projects,
    search_projects,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Stand up a fake workspace root with `.ai/atelier.db` migrated and
    `monkeypatch.chdir()` into it so `backend_local._workspace_root()`
    resolves to the tmp dir.

    The `scripts/projects.py` rewire dispatches through `backend`, which
    consults `mode_detector.detect_mode()`. On developer machines with
    Memex installed system-wide that resolves to `"memex"`, so we pin
    the mode to `"local"` for this suite — the local backend is the one
    `tests/test_projects.py` exercises end-to-end against the migrated
    SQLite file we just stood up.
    """
    from scripts import mode_detector
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    return str(db)


@pytest.fixture
def agent_id(db_path):
    role = create_role(db_path, name="pm", description="Project manager")
    agent = create_agent(db_path, id="pm-1", name="PM", role_id=role["id"],
                         profile="Experienced PM")
    return agent["id"]


def test_create_project(db_path, agent_id):
    project = create_project(db_path, name="Auth Service",
                             description="OAuth2 implementation",
                             repo="github.com/org/auth", created_by=agent_id)
    assert project["id"] == 1
    assert project["name"] == "Auth Service"
    # v1.1.0 schema default — was 'design:open' on legacy projects table too.
    assert project["phase"] == "design:open"
    assert project["created_by"] == agent_id
    # `repo` kwarg accepted but silently dropped; verify it's not on the row.
    assert "repo" not in project or project.get("repo") in (None,)


def test_get_project(db_path, agent_id):
    create_project(db_path, name="Auth Service", description="OAuth2",
                   created_by=agent_id)
    project = get_project(db_path, 1)
    assert project["name"] == "Auth Service"


def test_get_project_missing_returns_none(db_path):
    assert get_project(db_path, 999) is None


def test_update_project_phase(db_path, agent_id):
    create_project(db_path, name="Auth Service", description="OAuth2",
                   created_by=agent_id)
    updated = update_project(db_path, 1, phase="design:approved")
    assert updated["phase"] == "design:approved"


def test_delete_project(db_path, agent_id):
    create_project(db_path, name="Auth Service", description="OAuth2",
                   created_by=agent_id)
    assert delete_project(db_path, 1) is True
    assert get_project(db_path, 1) is None


def test_list_projects(db_path, agent_id):
    create_project(db_path, name="Auth Service", description="OAuth2",
                   created_by=agent_id)
    create_project(db_path, name="Payment API",
                   description="Stripe integration", created_by=agent_id)
    projects = list_projects(db_path)
    assert len(projects) == 2


def test_list_projects_filter_by_phase(db_path, agent_id):
    create_project(db_path, name="Auth Service", description="OAuth2",
                   created_by=agent_id)
    create_project(db_path, name="Payment API", description="Stripe",
                   created_by=agent_id)
    update_project(db_path, 2, phase="plan:open")
    results = list_projects(db_path, phase="design:open")
    assert len(results) == 1
    assert results[0]["name"] == "Auth Service"


def test_search_projects(db_path, agent_id):
    create_project(db_path, name="Auth Service",
                   description="OAuth2 implementation", created_by=agent_id)
    create_project(db_path, name="Payment API",
                   description="Stripe integration", created_by=agent_id)
    results = search_projects(db_path, query="OAuth")
    assert len(results) == 1
    assert results[0]["name"] == "Auth Service"

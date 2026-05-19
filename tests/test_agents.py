"""Tests for `scripts/agents.py` (Plan 3 Task 8 rewire).

Agents are now routed through the `scripts.backend` facade. The
`db_path` parameter is retained for back-compat but ignored — Local
mode resolves the DB via `backend_local._conn()` (workspace_root
+ `.ai/atelier.db`), Memex mode resolves `~/.memex/agents.db` via the
Memex registry. These tests pin Local-mode behavior; Memex-mode
coverage lives in the backend_memex / backend dispatch suites.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.agents import (
    create_agent,
    delete_agent,
    get_agent,
    list_agents,
    search_agents,
    update_agent,
)
from scripts.migrate import apply_migrations
from scripts.roles import create_role

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture(autouse=True)
def _force_local_mode():
    """Pin Local mode for every test in this module — the host may have a
    real `~/.memex/registry.json` that would otherwise route the facade
    through Memex's `agents.db`. These tests target the Local CRUD path
    against a fake workspace root in `tmp_path`."""
    with patch("scripts.mode_detector.detect_mode", return_value="local"):
        yield


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Stand up a fake workspace root with `.git/` + `.ai/atelier.db`
    migrated. `backend_local._conn()` reads `Path.cwd()` to find the
    workspace, so chdir into the fake root.
    """
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
def role_id(db_path):
    role = create_role(db_path, name="developer", description="Writes code")
    return role["id"]


def test_create_agent(db_path, role_id):
    agent = create_agent(
        db_path,
        id="dev-1",
        name="Alice",
        role_id=role_id,
        profile="Senior Python developer, 15 years experience",
    )
    assert agent["id"] == "dev-1"
    assert agent["name"] == "Alice"
    assert agent["role_id"] == role_id
    assert agent["profile"] == "Senior Python developer, 15 years experience"


def test_get_agent(db_path, role_id):
    create_agent(db_path, id="dev-1", name="Alice", role_id=role_id, profile="Expert")
    agent = get_agent(db_path, "dev-1")
    assert agent["name"] == "Alice"


def test_get_agent_missing_returns_none(db_path):
    assert get_agent(db_path, "nonexistent") is None


def test_update_agent(db_path, role_id):
    create_agent(db_path, id="dev-1", name="Alice", role_id=role_id, profile="Old profile")
    updated = update_agent(db_path, "dev-1", profile="Updated profile")
    assert updated["profile"] == "Updated profile"
    assert updated["name"] == "Alice"


def test_delete_agent(db_path, role_id):
    create_agent(db_path, id="dev-1", name="Alice", role_id=role_id, profile="Expert")
    assert delete_agent(db_path, "dev-1") is True
    assert get_agent(db_path, "dev-1") is None


def test_list_agents(db_path, role_id):
    create_agent(db_path, id="dev-1", name="Alice", role_id=role_id, profile="Expert")
    create_agent(db_path, id="dev-2", name="Bob", role_id=role_id, profile="Senior")
    agents = list_agents(db_path)
    assert len(agents) == 2


def test_list_agents_filter_by_role(db_path):
    r1 = create_role(db_path, name="developer", description="Writes code")["id"]
    r2 = create_role(db_path, name="qa", description="Tests code")["id"]
    create_agent(db_path, id="dev-1", name="Alice", role_id=r1, profile="Dev")
    create_agent(db_path, id="qa-1", name="Bob", role_id=r2, profile="QA")
    devs = list_agents(db_path, role_id=r1)
    assert len(devs) == 1
    assert devs[0]["id"] == "dev-1"


def test_search_agents(db_path, role_id):
    create_agent(db_path, id="dev-1", name="Alice", role_id=role_id, profile="Python expert")
    create_agent(db_path, id="dev-2", name="Bob", role_id=role_id, profile="Java developer")
    results = search_agents(db_path, query="Python")
    assert len(results) == 1
    assert results[0]["id"] == "dev-1"

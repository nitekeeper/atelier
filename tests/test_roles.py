"""Tests for `scripts/roles.py` (Plan 3 Task 7 rewire).

Roles are now routed through the `scripts.backend` facade. The
`db_path` parameter is retained for back-compat but ignored — Local
mode resolves the DB via `backend_local._local_db()` (workspace_root
+ `.ai/atelier.db`), Memex mode resolves `~/.memex/agents.db` via the
Memex registry. These tests pin Local-mode behavior; Memex-mode
coverage lives in the backend_memex / backend dispatch suites.
"""

from __future__ import annotations
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.migrate import apply_migrations
from scripts.roles import (
    create_role,
    delete_role,
    get_role,
    list_roles,
    search_roles,
    update_role,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture(autouse=True)
def _force_local_mode():
    """Pin `detect_mode()` to "local" for the whole module. Memex may be
    installed in the dev environment, which would otherwise route every
    call through `~/.memex/agents.db` and pollute the real store."""
    with patch("scripts.mode_detector.detect_mode", return_value="local"):
        yield


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Stand up a fake workspace root with `.git/` + `.ai/atelier.db`
    migrated. `backend_local._local_db()` reads `Path.cwd()` to find
    the workspace, so chdir into the fake root.
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


def test_create_role(db_path):
    role = create_role(db_path, name="developer", description="Writes and reviews code")
    assert role["id"] == 1
    assert role["name"] == "developer"
    assert role["description"] == "Writes and reviews code"
    assert role["created_at"]
    assert role["updated_at"]


def test_get_role(db_path):
    create_role(db_path, name="qa", description="Tests and validates")
    role = get_role(db_path, 1)
    assert role["name"] == "qa"


def test_get_role_missing_returns_none(db_path):
    assert get_role(db_path, 999) is None


def test_update_role(db_path):
    create_role(db_path, name="dev", description="Old description")
    updated = update_role(db_path, 1, description="New description")
    assert updated["description"] == "New description"
    assert updated["name"] == "dev"


def test_delete_role(db_path):
    create_role(db_path, name="pm", description="Manages projects")
    result = delete_role(db_path, 1)
    assert result is True
    assert get_role(db_path, 1) is None


def test_list_roles(db_path):
    create_role(db_path, name="developer", description="Writes code")
    create_role(db_path, name="qa", description="Tests code")
    roles = list_roles(db_path)
    assert len(roles) == 2


def test_search_roles(db_path):
    create_role(db_path, name="developer", description="Writes and reviews code")
    create_role(db_path, name="qa", description="Tests and validates")
    results = search_roles(db_path, query="code")
    assert len(results) == 1
    assert results[0]["name"] == "developer"


def test_create_role_is_idempotent(db_path):
    """`find_or_create_role` returns the existing row on duplicate name
    instead of raising IntegrityError. Description is NOT updated on
    hit — see backend_local.find_or_create_role docstring."""
    first = create_role(db_path, name="dev", description="Original")
    second = create_role(db_path, name="dev", description="Different")
    assert first["id"] == second["id"]
    assert second["description"] == "Original"

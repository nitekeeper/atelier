import pytest
from datetime import datetime, timezone
from scripts.db import get_connection
from scripts.migrate import apply_migrations
from scripts.roles import create_role, get_role, update_role, delete_role, list_roles, search_roles
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR / "shared")
    apply_migrations(path, MIGRATIONS_DIR / "local-only")
    return path

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

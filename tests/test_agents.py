import pytest
from pathlib import Path
from scripts.db import get_connection
from scripts.migrate import apply_migrations
from scripts.roles import create_role
from scripts.agents import create_agent, get_agent, update_agent, delete_agent, list_agents, search_agents

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR)
    return path

@pytest.fixture
def role_id(db_path):
    role = create_role(db_path, name="developer", description="Writes code")
    return role["id"]

def test_create_agent(db_path, role_id):
    agent = create_agent(db_path, id="dev-1", name="Alice", role_id=role_id,
                         profile="Senior Python developer, 15 years experience")
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

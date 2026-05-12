# tests/test_tasks.py
import pytest
from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.projects import create_project
from scripts.tasks import (create_task, get_task, update_task, delete_task,
                            assign_task, claim_task, complete_task,
                            list_tasks, search_tasks)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR)
    return path

@pytest.fixture
def setup(db_path):
    role = create_role(db_path, name="developer", description="Writes code")
    agent = create_agent(db_path, id="dev-1", name="Alice", role_id=role["id"], profile="Expert")
    project = create_project(db_path, name="Auth", description="OAuth2", created_by="dev-1")
    return {"db_path": db_path, "agent_id": agent["id"], "project_id": project["id"]}

def test_create_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    task = create_task(db, project_id=project_id, title="Write failing auth tests",
                       description="TDD red phase for JWT validation", created_by=agent_id)
    assert task["id"] == 1
    assert task["status"] == "pending"
    assert task["assigned_to"] is None

def test_get_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(db, project_id=project_id, title="Write tests", created_by=agent_id)
    task = get_task(db, 1)
    assert task["title"] == "Write tests"

def test_get_task_missing_returns_none(setup):
    assert get_task(setup["db_path"], 999) is None

def test_assign_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(db, project_id=project_id, title="Write tests", created_by=agent_id)
    task = assign_task(db, task_id=1, agent_id=agent_id)
    assert task["assigned_to"] == agent_id
    assert task["status"] == "assigned"

def test_claim_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(db, project_id=project_id, title="Write tests", created_by=agent_id)
    assign_task(db, task_id=1, agent_id=agent_id)
    task = claim_task(db, task_id=1, agent_id=agent_id)
    assert task["status"] == "in-progress"

def test_complete_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(db, project_id=project_id, title="Write tests", created_by=agent_id)
    assign_task(db, task_id=1, agent_id=agent_id)
    claim_task(db, task_id=1, agent_id=agent_id)
    task = complete_task(db, task_id=1)
    assert task["status"] == "complete"

def test_update_task_notes(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(db, project_id=project_id, title="Write tests", created_by=agent_id)
    task = update_task(db, 1, notes="Blocked on missing mock library")
    assert task["notes"] == "Blocked on missing mock library"

def test_delete_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(db, project_id=project_id, title="Write tests", created_by=agent_id)
    assert delete_task(db, 1) is True
    assert get_task(db, 1) is None

def test_list_tasks_by_status(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(db, project_id=project_id, title="Task A", created_by=agent_id)
    create_task(db, project_id=project_id, title="Task B", created_by=agent_id)
    assign_task(db, task_id=2, agent_id=agent_id)
    pending = list_tasks(db, status="pending")
    assert len(pending) == 1
    assert pending[0]["title"] == "Task A"

def test_search_tasks(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(db, project_id=project_id, title="Write JWT tests",
                description="Test token validation", created_by=agent_id)
    create_task(db, project_id=project_id, title="Fix login bug",
                description="Auth redirect broken", created_by=agent_id)
    results = search_tasks(db, query="JWT")
    assert len(results) == 1
    assert results[0]["title"] == "Write JWT tests"

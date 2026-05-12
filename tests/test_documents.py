# tests/test_documents.py
import pytest
from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.projects import create_project
from scripts.documents import (create_document, get_document, update_document,
                                delete_document, list_documents, search_documents)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR)
    return path

@pytest.fixture
def agent_id(db_path):
    role = create_role(db_path, name="pm", description="PM")
    agent = create_agent(db_path, id="pm-1", name="PM", role_id=role["id"], profile="Expert")
    return agent["id"]

@pytest.fixture
def project_id(db_path, agent_id):
    project = create_project(db_path, name="Auth Service", description="OAuth2", created_by=agent_id)
    return project["id"]

def test_create_document(db_path, project_id, agent_id):
    doc = create_document(db_path, project_id=project_id, type="design",
                          title="Auth Design Doc", filename="design/DESIGN.md", created_by=agent_id)
    assert doc["id"] == 1
    assert doc["project_id"] == project_id
    assert doc["type"] == "design"
    assert doc["filename"] == "design/DESIGN.md"

def test_get_document(db_path, project_id, agent_id):
    create_document(db_path, project_id=project_id, type="design",
                    title="Auth Design", filename="DESIGN.md", created_by=agent_id)
    doc = get_document(db_path, 1)
    assert doc["title"] == "Auth Design"

def test_get_document_missing_returns_none(db_path):
    assert get_document(db_path, 999) is None

def test_update_document(db_path, project_id, agent_id):
    create_document(db_path, project_id=project_id, type="design",
                    title="Old Title", filename="DESIGN.md", created_by=agent_id)
    updated = update_document(db_path, 1, title="New Title")
    assert updated["title"] == "New Title"

def test_delete_document(db_path, project_id, agent_id):
    create_document(db_path, project_id=project_id, type="design",
                    title="Auth Design", filename="DESIGN.md", created_by=agent_id)
    assert delete_document(db_path, 1) is True
    assert get_document(db_path, 1) is None

def test_list_documents_by_project(db_path, project_id, agent_id):
    create_document(db_path, project_id=project_id, type="design",
                    title="Design Doc", filename="DESIGN.md", created_by=agent_id)
    create_document(db_path, project_id=project_id, type="implementation-plan",
                    title="Impl Plan", filename="PLAN.md", created_by=agent_id)
    docs = list_documents(db_path, project_id=project_id)
    assert len(docs) == 2

def test_list_documents_by_type(db_path, project_id, agent_id):
    create_document(db_path, project_id=project_id, type="design",
                    title="Design Doc", filename="DESIGN.md", created_by=agent_id)
    create_document(db_path, project_id=project_id, type="implementation-plan",
                    title="Impl Plan", filename="PLAN.md", created_by=agent_id)
    docs = list_documents(db_path, type="design")
    assert len(docs) == 1
    assert docs[0]["type"] == "design"

def test_search_documents(db_path, project_id, agent_id):
    create_document(db_path, project_id=project_id, type="design",
                    title="OAuth2 Design", filename="DESIGN.md", created_by=agent_id)
    create_document(db_path, project_id=project_id, type="implementation-plan",
                    title="Stripe Plan", filename="PLAN.md", created_by=agent_id)
    results = search_documents(db_path, query="OAuth")
    assert len(results) == 1
    assert results[0]["title"] == "OAuth2 Design"

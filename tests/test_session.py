# tests/test_session.py
import pytest
from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.projects import create_project
from scripts.session import (
    write_session, get_session, read_latest,
    list_sessions, update_session, prune_sessions,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR / "shared")
    apply_migrations(path, MIGRATIONS_DIR / "local-only")
    role = create_role(path, name="pm", description="PM role")
    create_agent(path, id="pm-1", name="PM Agent", role_id=role["id"], profile="Expert PM")
    return path


@pytest.fixture
def project_id(db_path):
    project = create_project(db_path, name="TestProject", description="Test", created_by="pm-1")
    return project["id"]


def test_write_session_creates_row(db_path, project_id):
    session = write_session(
        db_path, project_id, "pm-1", "design:open", "in-progress",
        pm_notes="Starting fresh",
    )
    assert session["id"] is not None
    assert session["phase"] == "design:open"
    assert session["status"] == "in-progress"
    assert session["pm_notes"] == "Starting fresh"
    assert session["project_id"] == project_id


def test_write_session_stores_pre_diagnose_phase(db_path, project_id):
    session = write_session(
        db_path, project_id, "pm-1", "diagnose:open", "in-progress",
        pre_diagnose_phase="tdd:clean",
    )
    assert session["pre_diagnose_phase"] == "tdd:clean"


def test_read_latest_returns_most_recent(db_path, project_id):
    write_session(db_path, project_id, "pm-1", "design:open", "complete")
    write_session(db_path, project_id, "pm-1", "plan:open", "in-progress",
                  accomplished="Design approved")
    session = read_latest(db_path, project_id)
    assert session["phase"] == "plan:open"
    assert session["accomplished"] == "Design approved"


def test_read_latest_missing_project_returns_none(db_path):
    assert read_latest(db_path, 9999) is None


def test_list_sessions_returns_most_recent_first(db_path, project_id):
    write_session(db_path, project_id, "pm-1", "design:open", "complete")
    write_session(db_path, project_id, "pm-1", "plan:open", "complete")
    sessions = list_sessions(db_path, project_id)
    assert len(sessions) == 2
    assert sessions[0]["phase"] == "plan:open"


def test_list_sessions_respects_limit(db_path, project_id):
    for i in range(5):
        write_session(db_path, project_id, "pm-1", f"design:open", "complete",
                      pm_notes=f"session {i}")
    sessions = list_sessions(db_path, project_id, limit=3)
    assert len(sessions) == 3


def test_update_session_modifies_fields(db_path, project_id):
    session = write_session(db_path, project_id, "pm-1", "design:open", "in-progress")
    updated = update_session(db_path, session["id"],
                             status="complete", accomplished="Design done")
    assert updated["status"] == "complete"
    assert updated["accomplished"] == "Design done"


def test_update_session_rejects_unknown_fields(db_path, project_id):
    session = write_session(db_path, project_id, "pm-1", "design:open", "in-progress")
    # unknown fields silently ignored — ID should not change
    result = update_session(db_path, session["id"], unknown_field="value")
    assert result["id"] == session["id"]


def test_prune_sessions_keeps_n_most_recent(db_path, project_id):
    for i in range(5):
        write_session(db_path, project_id, "pm-1", "design:open", "complete",
                      pm_notes=f"session {i}")
    deleted = prune_sessions(db_path, project_id, keep=2)
    assert deleted == 3
    remaining = list_sessions(db_path, project_id)
    assert len(remaining) == 2


def test_prune_noop_when_fewer_than_keep(db_path, project_id):
    write_session(db_path, project_id, "pm-1", "design:open", "complete")
    deleted = prune_sessions(db_path, project_id, keep=5)
    assert deleted == 0

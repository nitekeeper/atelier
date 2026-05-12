# tests/test_meetings.py
import pytest
from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.meetings import (create_meeting, get_meeting, update_meeting,
                               delete_meeting, list_meetings, search_meetings,
                               add_participant, remove_participant, get_participants)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR)
    return path

@pytest.fixture
def meetings_dir(tmp_path):
    d = tmp_path / ".ai" / "meetings"
    d.mkdir(parents=True)
    return d

@pytest.fixture
def agent_id(db_path):
    role = create_role(db_path, name="pm", description="PM")
    agent = create_agent(db_path, id="pm-1", name="PM", role_id=role["id"], profile="Expert PM")
    return agent["id"]

def test_create_meeting_writes_db_record(db_path, meetings_dir, agent_id):
    meeting = create_meeting(db_path, meetings_dir, title="Sprint Planning",
                             date="2026-05-12", summary="Plan Q2 work",
                             decisions="Ship auth by end of May", created_by=agent_id)
    assert meeting["id"] == 1
    assert meeting["title"] == "Sprint Planning"
    assert meeting["filename"] == "2026-05-12-sprint-planning.md"

def test_create_meeting_writes_md_file(db_path, meetings_dir, agent_id):
    create_meeting(db_path, meetings_dir, title="Sprint Planning",
                   date="2026-05-12", summary="Plan Q2 work",
                   decisions="Ship auth by end of May", created_by=agent_id)
    md_file = meetings_dir / "2026-05-12-sprint-planning.md"
    assert md_file.exists()
    content = md_file.read_text()
    assert "Sprint Planning" in content
    assert "Plan Q2 work" in content

def test_get_meeting(db_path, meetings_dir, agent_id):
    create_meeting(db_path, meetings_dir, title="Sprint Planning",
                   date="2026-05-12", summary="Plan Q2", decisions="Ship", created_by=agent_id)
    meeting = get_meeting(db_path, 1)
    assert meeting["title"] == "Sprint Planning"

def test_get_meeting_missing_returns_none(db_path):
    assert get_meeting(db_path, 999) is None

def test_add_and_get_participants(db_path, meetings_dir, agent_id):
    create_meeting(db_path, meetings_dir, title="Standup", date="2026-05-12",
                   summary="Daily sync", decisions="", created_by=agent_id)
    role = create_role(db_path, name="dev", description="Developer")
    agent2 = create_agent(db_path, id="dev-1", name="Alice", role_id=role["id"], profile="Dev")
    add_participant(db_path, meeting_id=1, agent_id="pm-1")
    add_participant(db_path, meeting_id=1, agent_id="dev-1")
    participants = get_participants(db_path, 1)
    assert len(participants) == 2

def test_delete_meeting(db_path, meetings_dir, agent_id):
    create_meeting(db_path, meetings_dir, title="Sprint Planning",
                   date="2026-05-12", summary="Plan", decisions="", created_by=agent_id)
    assert delete_meeting(db_path, meetings_dir, 1) is True
    assert get_meeting(db_path, 1) is None
    assert not (meetings_dir / "2026-05-12-sprint-planning.md").exists()

def test_search_meetings(db_path, meetings_dir, agent_id):
    create_meeting(db_path, meetings_dir, title="Sprint Planning",
                   date="2026-05-12", summary="Q2 roadmap", decisions="", created_by=agent_id)
    create_meeting(db_path, meetings_dir, title="Standup",
                   date="2026-05-12", summary="Daily sync", decisions="", created_by=agent_id)
    results = search_meetings(db_path, query="roadmap")
    assert len(results) == 1
    assert results[0]["title"] == "Sprint Planning"

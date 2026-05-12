# tests/test_workflow.py
import pytest
from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.projects import create_project, get_project
from scripts.workflow import (
    get_phase, advance_phase, check_gate,
    PHASE_GATES, VALID_TRANSITIONS, WorkflowError
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR)
    role = create_role(path, name="pm", description="PM")
    create_agent(path, id="pm-1", name="PM", role_id=role["id"], profile="Expert PM")
    return path

@pytest.fixture
def project_id(db_path):
    project = create_project(db_path, name="Auth", description="OAuth2", created_by="pm-1")
    return project["id"]

def test_get_phase_returns_current_phase(db_path, project_id):
    assert get_phase(db_path, project_id) == "design:in-progress"

def test_advance_phase_design_to_approved(db_path, project_id):
    advance_phase(db_path, project_id, "design:approved")
    assert get_phase(db_path, project_id) == "design:approved"

def test_advance_phase_invalid_transition_raises(db_path, project_id):
    with pytest.raises(WorkflowError, match="Invalid transition"):
        advance_phase(db_path, project_id, "qa-review:approved")

def test_check_gate_passes_when_met(db_path, project_id):
    advance_phase(db_path, project_id, "design:approved")
    check_gate(db_path, project_id, required_phase="design:approved")

def test_check_gate_fails_when_not_met(db_path, project_id):
    with pytest.raises(WorkflowError, match="Gate not met"):
        check_gate(db_path, project_id, required_phase="design:approved")

def test_full_happy_path_transitions(db_path, project_id):
    transitions = [
        "design:approved",
        "plan:in-progress",
        "plan:approved",
        "tdd:red",
        "tdd:green",
        "tdd:refactor",
        "code-review:draft",
        "code-review:merged",
        "security-review:in-progress",
        "security-review:approved",
        "qa-review:in-progress",
        "qa-review:approved",
    ]
    for phase in transitions:
        advance_phase(db_path, project_id, phase)
        assert get_phase(db_path, project_id) == phase

def test_diagnose_can_be_entered_from_any_phase(db_path, project_id):
    check_gate(db_path, project_id, required_phase=None)

def test_advance_to_code_review_changes_requested(db_path, project_id):
    for phase in ["design:approved", "plan:in-progress", "plan:approved",
                  "tdd:red", "tdd:green", "tdd:refactor", "code-review:draft"]:
        advance_phase(db_path, project_id, phase)
    advance_phase(db_path, project_id, "code-review:changes-requested")
    assert get_phase(db_path, project_id) == "code-review:changes-requested"

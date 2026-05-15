"""Soft walls: check_gate returns GateResult instead of raising."""
import pytest

from scripts.migrate import apply_migrations, MIGRATIONS_DIR
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.projects import create_project
from scripts import workflow


@pytest.fixture
def fresh_db(tmp_path):
    """Migrate a fresh DB and return the path string."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    role = create_role(db_path, name="pm", description="PM")
    create_agent(db_path, id="test-agent", name="Test Agent", role_id=role["id"], profile="Tester")
    return db_path


@pytest.fixture
def project(fresh_db):
    """Create a project; returns (db_path, project_id). Starts at design:open."""
    proj = create_project(fresh_db, name="test", description=None, created_by="test-agent")
    return fresh_db, proj["id"]


def test_check_gate_returns_gate_result_when_allowed(project):
    """check_gate on an ungated skill returns GateResult with allowed=True and no required_phase."""
    db_path, project_id = project
    # dev:design has no gate — always allowed
    result = workflow.check_gate(db_path, project_id, "dev:design")
    assert result.allowed is True
    assert result.current_phase == "design:open"
    assert result.required_phase is None
    assert "no gate" in result.reason.lower()


def test_check_gate_returns_gate_result_when_phase_satisfies(project):
    """check_gate returns allowed=True when the project is already at the required phase."""
    db_path, project_id = project
    # Advance to design:approved so dev:plan is allowed
    workflow.advance_phase(db_path, project_id, "design:approved")
    result = workflow.check_gate(db_path, project_id, "dev:plan")
    assert result.allowed is True
    assert result.current_phase == "design:approved"
    assert result.required_phase == "design:approved"


def test_check_gate_does_not_raise_on_mismatch(project):
    """The old behavior raised WorkflowError. New behavior returns GateResult with allowed=False."""
    db_path, project_id = project
    # Project is at design:open; dev:plan requires design:approved
    result = workflow.check_gate(db_path, project_id, "dev:plan")
    assert result.allowed is False
    assert result.current_phase == "design:open"
    assert result.required_phase == "design:approved"
    assert "design:open" in result.reason
    assert "design:approved" in result.reason


def test_check_gate_raises_on_unknown_project_id(fresh_db):
    """Documents the exception path: unknown project_id raises WorkflowError.

    The 'never raises on phase mismatch' contract does not cover invalid input.
    """
    with pytest.raises(workflow.WorkflowError, match="not found"):
        workflow.check_gate(fresh_db, 9999, "dev:plan")

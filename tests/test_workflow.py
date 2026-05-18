# tests/test_workflow.py
import pytest
from scripts.migrate import apply_migrations, MIGRATIONS_DIR
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.projects import create_project
from scripts.workflow import (
    get_phase, advance_phase, check_gate,
    get_valid_transitions, is_allow_from_any, WorkflowError, GateResult,
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR / "shared")
    apply_migrations(path, MIGRATIONS_DIR / "local-only")
    role = create_role(path, name="pm", description="PM")
    create_agent(path, id="pm-1", name="PM", role_id=role["id"], profile="Expert PM")
    return path


@pytest.fixture
def project_id(db_path):
    project = create_project(db_path, name="Auth", description="OAuth2 login", created_by="pm-1")
    return project["id"]


def test_get_phase_returns_default_design_open(db_path, project_id):
    assert get_phase(db_path, project_id) == "design:open"


def test_get_phase_unknown_project_raises(db_path):
    with pytest.raises(WorkflowError, match="not found"):
        get_phase(db_path, 9999)


def test_advance_phase_valid_transition(db_path, project_id):
    advance_phase(db_path, project_id, "design:approved")
    assert get_phase(db_path, project_id) == "design:approved"


def test_advance_phase_invalid_transition_raises(db_path, project_id):
    with pytest.raises(WorkflowError, match="Invalid transition"):
        advance_phase(db_path, project_id, "qa:approved")


def test_check_gate_no_gate_always_passes(db_path, project_id):
    """check_gate on an ungated skill returns GateResult(allowed=True, required_phase=None)."""
    # dev:design has no gate — passes even at design:open
    result = check_gate(db_path, project_id, "dev:design")
    assert isinstance(result, GateResult)
    assert result.allowed is True
    assert result.required_phase is None


def test_check_gate_passes_when_met(db_path, project_id):
    """check_gate returns allowed=True when the project satisfies the required phase."""
    advance_phase(db_path, project_id, "design:approved")
    result = check_gate(db_path, project_id, "dev:plan")
    assert isinstance(result, GateResult)
    assert result.allowed is True
    assert result.required_phase == "design:approved"


def test_check_gate_fails_when_not_met(db_path, project_id):
    """check_gate returns GateResult(allowed=False) instead of raising WorkflowError."""
    # project is at design:open, dev:plan requires design:approved
    result = check_gate(db_path, project_id, "dev:plan")
    assert isinstance(result, GateResult)
    assert result.allowed is False
    assert result.current_phase == "design:open"
    assert result.required_phase == "design:approved"


def test_diagnose_allow_from_any_is_true(db_path):
    assert is_allow_from_any(db_path, "diagnose:open") is True


def test_design_open_allow_from_any_is_false(db_path):
    assert is_allow_from_any(db_path, "design:open") is False


def test_advance_to_diagnose_from_any_phase(db_path, project_id):
    # Project is at design:open (default) — can still enter diagnose:open
    advance_phase(db_path, project_id, "diagnose:open")
    assert get_phase(db_path, project_id) == "diagnose:open"


def test_get_valid_transitions_from_design_open(db_path):
    transitions = get_valid_transitions(db_path, "design:open")
    assert "design:approved" in transitions
    assert len(transitions) == 1


def test_full_happy_path(db_path, project_id):
    path = [
        "design:approved", "plan:open", "plan:approved",
        "tdd:red", "tdd:green", "tdd:clean",
        "review:open", "review:approved",
        "security:open", "security:approved",
        "qa:open", "qa:approved",
        "handoff:complete",
    ]
    for phase in path:
        advance_phase(db_path, project_id, phase)
        assert get_phase(db_path, project_id) == phase


def test_review_changes_requested_loop(db_path, project_id):
    for phase in [
        "design:approved", "plan:open", "plan:approved",
        "tdd:red", "tdd:green", "tdd:clean", "review:open",
    ]:
        advance_phase(db_path, project_id, phase)
    advance_phase(db_path, project_id, "review:changes-requested")
    advance_phase(db_path, project_id, "review:open")
    assert get_phase(db_path, project_id) == "review:open"


def test_tdd_cycle_repeats(db_path, project_id):
    for phase in ["design:approved", "plan:open", "plan:approved",
                  "tdd:red", "tdd:green", "tdd:clean"]:
        advance_phase(db_path, project_id, phase)
    advance_phase(db_path, project_id, "tdd:red")
    assert get_phase(db_path, project_id) == "tdd:red"


def test_diagnose_resolved_returns_to_prior_phase(db_path, project_id):
    advance_phase(db_path, project_id, "diagnose:open")
    advance_phase(db_path, project_id, "diagnose:resolved")
    advance_phase(db_path, project_id, "design:open")  # restore via PM-recorded pre_diagnose_phase
    assert get_phase(db_path, project_id) == "design:open"

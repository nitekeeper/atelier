# tests/test_backend_memex_state.py
"""Tests for Plan 2 Task 2 — Memex-mode operational state writes.

These are the Tier 1 writes that bypass the Librarian path entirely:
upsert_session / transition_phase / update_task_status /
record_phase_bypass go straight through Memex Core CRUD
(scripts.stores.insert/update/query).
"""

from unittest.mock import patch

import pytest

from scripts import backend_memex


@pytest.fixture
def mock_core():
    """Patch the Memex Core dispatch helpers so tests don't need a real
    ~/.memex/atelier.db on disk."""
    with (
        patch.object(backend_memex, "_memex_core_insert") as ins,
        patch.object(backend_memex, "_memex_core_update") as upd,
        patch.object(backend_memex, "_memex_core_query") as qry,
    ):
        yield {"insert": ins, "update": upd, "query": qry}


# ── upsert_session ───────────────────────────────────────────────────────


def test_upsert_session_inserts_when_new(mock_core):
    """No in-progress session for (project_id, agent_id) → INSERT path."""

    # Query is called twice in the INSERT branch:
    #   1) sessions lookup (existing in-progress) → must be empty
    #   2) projects lookup to derive workspace_id (issue #6 bug #2)
    def query_side_effect(*, store, table, where=None):
        if table == "projects":
            return [{"id": 1, "workspace_id": 5, "slug": "myproj"}]
        return []

    mock_core["query"].side_effect = query_side_effect
    mock_core["insert"].return_value = {
        "id": 1,
        "project_id": 1,
        "agent_id": "atelier-pm-1",
        "phase": "design:open",
        "workspace_id": 5,
    }
    r = backend_memex.upsert_session(
        project_id=1,
        agent_id="atelier-pm-1",
        phase="design:open",
        current_tasks="onboarding",
        accomplished="",
        next_action="grill design",
    )
    assert r["id"] == 1
    mock_core["insert"].assert_called_once()
    # The insert must target the sessions table on the atelier store.
    kwargs = mock_core["insert"].call_args.kwargs
    assert kwargs["store"] == "atelier"
    assert kwargs["table"] == "sessions"
    # And the row body must carry the identity columns we used for lookup.
    assert kwargs["row"]["project_id"] == 1
    assert kwargs["row"]["agent_id"] == "atelier-pm-1"
    # NOT NULL workspace_id is derived from the project lookup.
    assert kwargs["row"]["workspace_id"] == 5


def test_upsert_session_updates_when_existing(mock_core):
    """An in-progress row exists → UPDATE path; insert MUST NOT fire."""
    mock_core["query"].return_value = [
        {"id": 7, "project_id": 1, "agent_id": "atelier-pm-1", "status": "in-progress"}
    ]
    mock_core["update"].return_value = {
        "id": 7,
        "project_id": 1,
        "agent_id": "atelier-pm-1",
        "accomplished": "finished kickoff",
        "status": "in-progress",
    }
    r = backend_memex.upsert_session(
        project_id=1,
        agent_id="atelier-pm-1",
        accomplished="finished kickoff",
    )
    assert r["id"] == 7
    mock_core["update"].assert_called_once()
    mock_core["insert"].assert_not_called()
    # The update must hit the existing row's id, NOT (project_id, agent_id).
    kwargs = mock_core["update"].call_args.kwargs
    assert kwargs["row_id"] == 7
    assert kwargs["table"] == "sessions"


# ── transition_phase ─────────────────────────────────────────────────────


def test_transition_phase_writes_to_projects_phase_column(mock_core):
    """Project row located by id → phase column updated in the projects
    table. Spec §4.3 puts the canonical phase on projects.phase, not on
    sessions — sessions snapshot a phase for retro purposes only."""
    mock_core["query"].return_value = [{"id": 1, "phase": "design:approved"}]
    mock_core["update"].return_value = {"id": 1, "phase": "plan:open"}

    r = backend_memex.transition_phase(
        project_id=1,
        to_phase="plan:open",
        agent_id="atelier-pm-1",
    )
    assert r["phase"] == "plan:open"
    # The update target is the projects row, not sessions.
    kwargs = mock_core["update"].call_args.kwargs
    assert kwargs["table"] == "projects"
    assert kwargs["row_id"] == 1
    assert kwargs["changes"] == {"phase": "plan:open"}


# ── update_task_status ───────────────────────────────────────────────────


def test_update_task_status_writes_status_column(mock_core):
    """Status update goes through Core update; the row_id is the task.id."""
    mock_core["update"].return_value = {"id": 1, "status": "in-progress"}
    r = backend_memex.update_task_status(task_id=1, status="in-progress")
    assert r["status"] == "in-progress"
    kwargs = mock_core["update"].call_args.kwargs
    assert kwargs["table"] == "tasks"
    assert kwargs["row_id"] == 1
    assert kwargs["changes"]["status"] == "in-progress"


def test_record_phase_bypass_inserts_row(mock_core):
    """Bypass logged as a new row in phase_bypasses with all five cols."""
    mock_core["insert"].return_value = {
        "id": 1,
        "project_id": 1,
        "from_phase": "design:open",
        "to_phase": "plan:open",
        "reason": "user override",
        "agent_id": "atelier-pm-1",
    }
    r = backend_memex.record_phase_bypass(
        project_id=1,
        from_phase="design:open",
        to_phase="plan:open",
        reason="user override",
        agent_id="atelier-pm-1",
    )
    assert r["id"] == 1
    kwargs = mock_core["insert"].call_args.kwargs
    assert kwargs["table"] == "phase_bypasses"
    assert kwargs["row"]["project_id"] == 1
    assert kwargs["row"]["from_phase"] == "design:open"
    assert kwargs["row"]["to_phase"] == "plan:open"
    assert kwargs["row"]["reason"] == "user override"
    assert kwargs["row"]["agent_id"] == "atelier-pm-1"
